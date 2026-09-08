# Releasing eve-skills

How to go from a working tree to a tagged, installable release. Nothing here is automated:
every step is a command you can read before you run it.

Two toolchains work and produce the same artifacts: **uv** or a plain virtualenv with `pip`. Pick one per
run — mixing them is how an install lands in the wrong environment. Proven here with uv 0.12.6 on
Linux: `uv venv`, `uv pip install -e .`, `uv run eve-skills --version`,
`uv run python -m unittest discover -s tests -t . -q`, `uv build`, and `uv tool install .` /
`uv tool list` / `uv tool uninstall eve-skills`. Not proven here — documented forms written from
`uv --help`: `uv run --with setuptools …`, `uvx twine check …`, and the `git+https://…` tool install in
step 6. Nothing in either toolchain has ever been run on Windows.

Status today: **no release has been published yet** — `git tag` is empty, so `0.1.0` in
`eve_skills/__init__.py` is the current *development* version, not a shipped one. The source
lives at <https://github.com/kirilan/eve-skills> (remote `origin`, branch `main`); no package
index account is configured, so `twine upload` below stays optional.

## Versioning

SemVer (`MAJOR.MINOR.PATCH`) with one source of truth:

```python
eve_skills/__init__.py -> __version__ = "0.1.0"
```

`pyproject.toml` declares `dynamic = ["version"]` and reads that attribute
(`[tool.setuptools.dynamic]`), so the built metadata, `eve-skills --version` and
`eve-skills doctor` can never disagree. Bump **only** that line.

The public surface a bump has to respect is what a user or script observes: command names,
flags, the `--json` documents, the CSV headers, exit codes, and the on-disk layout — the `$XDG_*`
tree on POSIX and the `%LOCALAPPDATA%\eve-skills` tree on Windows. Moving or renaming a
file in either of them is a breaking change for anyone who backs one up. The documents that live
there are `tokens.json`, `config.json` and `sp-history.jsonl` (config), `watch-state.json` and
`events.jsonl` (state), `endpoints.json`, `names.json`, `types.json` and `quotes.json` (cache), and
the three bundled SDE documents under `eve_skills/data/`. The two newest cache documents —
`types.json`, the type/group/category catalogue `inventory` builds, and `quotes.json`, the order-book
figures behind `inventory --value-at` — are `version`-tagged inside, so a user may delete either to
force a refetch; that is not a breaking change. A release moving or renaming one of them is.

| Change | While `0.y.z` (now) | From `1.0.0` |
|---|---|---|
| Bug fix, no surface change; refreshed bundled SDE snapshot | `PATCH` | `PATCH` |
| New command/flag/JSON key, backwards compatible | `MINOR` | `MINOR` |
| Renamed or removed flag, changed JSON key/type, changed CSV header, new required config | `MINOR` + a written "breaking" note | `MAJOR` |

## 1. Pre-flight

With uv, no environment to make first:

```bash
cd eve-skills                                              # your checkout
git status --short                    # clean tree apart from what you intend to release
uv run --with setuptools python -m unittest discover -s tests -t . -q   # the whole suite: 462 tests
uv run eve-skills doctor                                   # version, data freshness, config
```

**Read the skip count before you believe `OK`.** `tests/test_packaging.py` builds real wheel and sdist
and installs them into a throwaway venv, and it *skips that tier* when the interpreter running the
suite has no `setuptools` — which is exactly the state of a uv-managed Python. The project's own
dependencies are empty, so this cannot be fixed by installing the project: add the build backend to the
run with `uv run --with setuptools python -m unittest discover -s tests -t . -q`, or install it into the
interpreter as in the venv route below. A green suite that skipped its packaging tier has not checked
that this tree packages.

With a plain virtualenv:

```bash
cd eve-skills                      # your checkout
.venv/bin/pip install setuptools   # the packaging tier builds real artifacts; it skips without it
git status --short                 # clean tree apart from what you intend to release
.venv/bin/python -m unittest       # full suite, including the packaging tier
.venv/bin/eve-skills doctor        # offline checks: version, data freshness, config
```

`doctor` reports which SDE build ships inside `eve_skills/data/` (origin *bundled package*).
A fresh install gets exactly that snapshot, so note the build number in the release notes.

## 2. Bump the version

```bash
V=0.2.0
sed -i "s/^__version__ = .*/__version__ = \"$V\"/" eve_skills/__init__.py
.venv/bin/eve-skills --version          # must print: eve-skills $V   (or: uv run eve-skills --version)
git diff                            # one line, nothing else
```

`sed` has no honest PowerShell twin, so on Windows open the file and change that one line yourself —
the check afterwards is the same (`uv run eve-skills --version`, or `.venv\Scripts\eve-skills.exe
--version`).

## 3. Build

With uv, one command and no install step — uv provisions its own isolated build environment, so the
release extra is not needed to build:

```bash
rm -rf dist
uv build                            # -> dist/eve_skills-$V-py3-none-any.whl + .tar.gz
```

With pip, build tooling is an optional extra — the package itself has no runtime dependencies:

```bash
.venv/bin/pip install -e ".[release]"   # release tooling only; the package itself needs nothing
rm -rf dist build *.egg-info
.venv/bin/python -m build               # -> the same two artifacts
```

No `build`, or no isolated build available? The PEP 517 hooks work with an already-installed
setuptools and need no network. Run them as **two separate interpreters** — calling both from one
process makes setuptools leave the sdist in a stray `bdist_wheel/` directory instead of `dist/`.
A Python 3.14 virtualenv has no setuptools of its own, so use whichever interpreter has it:

```bash
python3 -c 'import setuptools.build_meta as b; b.build_wheel("dist")' > /dev/null
python3 -c 'import setuptools.build_meta as b; b.build_sdist("dist")' > /dev/null
```

Artifacts land in `dist/`, which `.gitignore` excludes — never commit them.

## 4. Inspect the artifacts

```
python3 -m zipfile -l "dist/eve_skills-$V-py3-none-any.whl"  # every eve_skills/*.py + data/*.json
unzip -p "dist/eve_skills-$V-py3-none-any.whl" '*/METADATA'  # Version, License-Expression: GPL-3.0-only
unzip -p "dist/eve_skills-$V-py3-none-any.whl" '*/entry_points.txt'   # eve-skills = eve_skills.cli:main
tar tzf "dist/eve_skills-$V.tar.gz"                          # sources + README.md + LICENSE + data/
twine check "dist/eve_skills-$V"*                             # metadata renders (optional)
```

Expect in both artifacts: every `eve_skills/*.py` module (the packaging tests derive the list from
the source tree, so compare against `ls eve_skills/*.py`), the three SDE
documents under `eve_skills/data/`, `README.md` and the verbatim `LICENSE`. In the wheel METADATA
expect `License-Expression: GPL-3.0-only`, and no `Requires-Dist` line **without** an
`extra == "release"` marker — an unmarked one means a runtime dependency crept in (the optional
release tooling may appear, marked).

`twine` is optional and needs no install under uv — `uvx twine check "dist/eve_skills-$V"*` runs it in a
throwaway environment.

## 5. Clean-install smoke test

Install the wheel into a throwaway environment that cannot see this checkout:

```bash
T=$(mktemp -d)
python3 -m venv "$T/venv"
"$T/venv/bin/pip" install --no-index --no-deps "dist/eve_skills-$V-py3-none-any.whl"
cd "$T"                                        # out of the source tree, so imports cannot leak
HOME="$T/home" XDG_CONFIG_HOME="$T/cfg" XDG_CACHE_HOME="$T/cache" XDG_DATA_HOME="$T/data" \
  "$T/venv/bin/eve-skills" --version
HOME="$T/home" XDG_CONFIG_HOME="$T/cfg" XDG_CACHE_HOME="$T/cache" XDG_DATA_HOME="$T/data" \
  "$T/venv/bin/python" -m eve_skills --version   # identical output to the console script
HOME="$T/home" XDG_CONFIG_HOME="$T/cfg" XDG_CACHE_HOME="$T/cache" XDG_DATA_HOME="$T/data" \
  "$T/venv/bin/eve-skills" doctor --json > "$T/doctor.json"   # exit 1 is expected: nobody logged in
python3 - "$T/doctor.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
print("package:", report["versions"]["package"])
for check in report["checks"]:
    if check["name"].startswith("data."):
        print(f'{check["name"]:<20} {check["status"]:<4} {check.get("origin", "")}')
PY
rm -rf "$T"
```

The same pass on Windows, where three details of that block do not transfer. The venv's script
directory is `Scripts`, not `bin`; the console entry point is a real `eve-skills.exe`; and there is no
`HOME=… XDG_*=… command` prefix form — the variables have to be set in the session first, which works
because `$XDG_*` pins are honoured on Windows too (see [README — The four
roots](README.md#the-four-roots)), so this run still tests a throwaway tree rather than the real profile:

```powershell
$V = "0.2.0"
$T = Join-Path $env:TEMP "eve-skills-smoke-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $T | Out-Null
python -m venv "$T\venv"
& "$T\venv\Scripts\pip.exe" install --no-index --no-deps "dist\eve_skills-$V-py3-none-any.whl"
cd $T                                                   # out of the source tree, so imports cannot leak
$env:HOME = "$T\home"
$env:XDG_CONFIG_HOME = "$T\cfg"
$env:XDG_CACHE_HOME = "$T\cache"
$env:XDG_DATA_HOME = "$T\data"
& "$T\venv\Scripts\eve-skills.exe" --version
& "$T\venv\Scripts\python.exe" -m eve_skills --version   # identical output to the console script
& "$T\venv\Scripts\eve-skills.exe" doctor                # exit 1 expected: nobody logged in
Remove-Item -Recurse -Force $T
```

If you capture `doctor --json` to a file on Windows rather than reading it in the terminal, pick the
encoding deliberately: Windows PowerShell 5.1's `>` writes UTF-16LE, `pwsh` 7 and later write UTF-8, and
the snippet above opens the file as plain text. Reading it in the terminal avoids the question. Note too
that on this platform `doctor` legitimately reports six checks **skipped** — the mode-bit ones — and
`versions.platform` names the OS, which is exactly what a release report should show if you run the
smoke test there.

The two `--version` runs must match, and every `data.*` row must be `ok` — with origin *bundled
package* wherever an origin is shown (`skill_catalog` prints a path instead). Either way the data
came from inside the wheel, not from this working copy. A fresh install legitimately exits 1
overall: no application client id is configured and no character is logged in, and `doctor` calls
both problems. What must not appear is a `data.*` row resolved from this checkout, or missing.

## 6. Tag

```bash
git add eve_skills/__init__.py            # plus any files the release actually changed
git commit -m "Release $V"
git tag -a "v$V" -m "eve-skills $V"
```

The repository has a remote, so tags and commits publish with:

```bash
git push origin main --follow-tags
```

A package index is *not* configured: `twine upload` needs credentials this project has never
had, and it is only worth doing if you want `pip install eve-skills` to work without the
GitHub URL.

```bash
twine upload "dist/eve_skills-$V"*        # only if you also publish to an index
```

Then check the tag itself is what a user would get, from a clean shell and without this checkout on the
path:

```bash
uv tool install "eve-skills @ git+https://github.com/kirilan/eve-skills.git@v$V"
eve-skills --version && eve-skills doctor          # then: uv tool uninstall eve-skills
```

That is the only route a user has while no index exists, so it is worth one run per release on each
platform you claim — and on Windows it is the only step here that would show whether the console script
really lands somewhere on `PATH` (`uv tool update-shell` if it does not), since nothing else in this
procedure runs the installed command rather than a venv-relative path.

Never hand-edit `dist/` contents and re-tag: rebuild from the tagged commit
(`git archive` the tag into a clean directory, then repeat steps 3–5) so the artifact matches
the source it claims.
