# Releasing eve-skills

How to go from a working tree to a tagged, installable release. Nothing here is automated:
every step is a command you can read before you run it.

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
flags, the `--json` documents, the CSV headers, exit codes, and the `$XDG_*` file layouts.

| Change | While `0.y.z` (now) | From `1.0.0` |
|---|---|---|
| Bug fix, no surface change; refreshed bundled SDE snapshot | `PATCH` | `PATCH` |
| New command/flag/JSON key, backwards compatible | `MINOR` | `MINOR` |
| Renamed or removed flag, changed JSON key/type, changed CSV header, new required config | `MINOR` + a written "breaking" note | `MAJOR` |

## 1. Pre-flight

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
.venv/bin/eve-skills --version          # must print: eve-skills $V
git diff                            # one line, nothing else
```

## 3. Build

Build tooling is an optional extra — the package itself has no runtime dependencies:

```bash
.venv/bin/pip install -e ".[release]"   # release tooling only; the package itself needs nothing
rm -rf dist build *.egg-info
.venv/bin/python -m build               # -> dist/eve_skills-$V-py3-none-any.whl + .tar.gz
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

Never hand-edit `dist/` contents and re-tag: rebuild from the tagged commit
(`git archive` the tag into a clean directory, then repeat steps 3–5) so the artifact matches
the source it claims.
