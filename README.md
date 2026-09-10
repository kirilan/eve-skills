# eve-skills

Command line tool for EVE Online characters: skills, training queue and alpha/omega clone state,
plus opt-in views for standings, industry jobs, asset inventory (named, placed and valued), location/jump
clones, implants, training plans and Skill Extractor math - and market data: live order books and prices
for any item in any region or trade hub, what manufacturing that item would cost instead, your own open
and closed orders, and a watch that announces the moment one of them fills, expires or is cancelled.

- Linux and Windows, Python 3.11+, **standard library only** — no runtime dependencies, nothing to
  compile. See [Platform support](#platform-support).
- Read-only against ESI. It never spends ISK, moves ships, trains skills or changes anything.
- Multi-character: one login per character, every command walks all stored characters unless you
  pick one with `--char`.
- Text output by default, with `--json` / `--csv` for scripting and `--watch` for a live terminal
  dashboard.

```console
$ eve-skills                     # same as: eve-skills skills
$ eve-skills summary
$ eve-skills skills --char Somecharacter --filter omega --sort sp
$ eve-skills market Tritanium --hub jita --history 30   # public prices; no login needed
$ eve-skills build-cost Hound                            # build it or buy it, off the same order books
$ eve-skills orders --watch 1                            # announce my own fills as they happen
```

---

## What it shows

| Command | Purpose | Extra consent needed |
|---|---|---|
| `skills` (default) | Clone state + evidence, totals, training queue, trained-skill table | no (core login) |
| `summary` | One row per character: clone state, total SP, queue length, current item time left, grand total | no |
| `attributes` | Base attributes, remaps available/last remap, accelerator days | no — covered by the standard skills consent |
| `market` | Live order book for any type: best sell/buy, spread, margin, listed volume - per region, at a station-level trade hub, or across the cluster with `--global`; `--history DAYS` adds traded volume | no — public ESI; works before you have logged in |
| `build-cost` | What manufacturing one item costs right now: per-material buy-or-build table, the install fee with its arithmetic shown, and the same unit bought instead as a verdict; `--runs`, `--me`/`--te`/`--component-me`, `--build`/`--buy`, `--hub`/`--region`/`--system` | no — public ESI; recipes come from local SDE data (`update-data`) |
| `chars` | Stored characters, access-token time left, auto-refresh availability | offline (no network) |
| `events` | Recorded watch alerts: training finished / queue emptied / your orders filled, expired or cancelled | offline (no network) |
| `standings` | Agent / NPC corp / faction standings | `--scopes standings` |
| `jobs` | Personal or `--corp` industry jobs | `--scopes jobs` |
| `orders` | Your own open orders with price, remaining volume, escrow and time left; `--closed` for ESI's ~90-day order history; `--watch` announces fills/expiries; `--corp` for corporation orders | `--scopes orders` (and `corp-orders` for `--corp`) |
| `inventory` | Assets named, placed and valued: per-location summary with subtotals, the same table turned round with `--by category`, one row per item with `--items`, a real standing bid with `--value-at jita`, or full `--csv` | `--scopes assets`; plus `structures` to name player-owned structures |
| `travel` | Current location, home, jump clones with their implants | `--scopes location` and/or `clones` |
| `implants` | Implants fitted in the active clone | `--scopes clones` |
| `plan` | Ordered, priced training path to target levels incl. auto-added prerequisites | no — needs the SDE skill catalog (`update-data`) |
| `extract` | Skill Extractor count, injector yield, re-training cost | no |
| `update-data` | Refresh alpha caps, the full skill catalog and every blueprint's material list from the official SDE (~100 MB download) | no |
| `doctor` | Diagnose install, stored logins and data freshness; `--network` probes SSO/ESI | no |

Run `eve-skills <command> --help` for the full option list of any command. Errors print
`error: <reason>` on stderr and exit 1; a missing optional consent is *not* an error — see the
consent notes in the [login section](#adding-characters-extra-consent-logging-out). `doctor` is
the one handler with its own exit semantics: it also exits 1 when a check reports a blocking
problem.

---

## Install

Python 3.11 or newer, standard library only — no runtime dependencies and nothing to compile. Linux
and Windows are both supported; [Platform support](#platform-support) says exactly what differs.

### With uv

[uv](https://docs.astral.sh/uv/) is the shortest path, and it needs no pre-existing virtualenv.

Try it with nothing installed:

```bash
git clone https://github.com/kirilan/eve-skills.git
cd eve-skills
uv run eve-skills --version                              # -> eve-skills 0.1.0; no install step
uv run eve-skills market Tritanium --hub jita            # public prices, before any login
uv run --with setuptools python -m unittest discover -s tests -t . -q   # the whole suite
```

`--with setuptools` is not decoration: the packaging tier builds a real wheel and sdist to inspect,
so without a build backend its two build-and-install classes skip their seven tests and the run
reports 513 instead of 520.

Keep a project environment in sync with the lockfile — this is the install and update path:

```bash
uv sync                            # creates .venv and installs the project (and updates it later)
uv sync --extra release            # …plus the build/twine tooling RELEASE.md uses
uv run eve-skills chars            # runs against that environment
```

Install it as a tool on your `PATH` for daily use:

```bash
uv tool install .                                                # from this checkout
uv tool install git+https://github.com/kirilan/eve-skills.git    # …or straight from GitHub
uv tool list                                                     # -> eve-skills v0.1.0
uv tool uninstall eve-skills                                     # …and take it off again
```

The console script lands in uv's own tool directory (`~/.local/bin/eve-skills` on this machine); if
the shell cannot find `eve-skills` afterwards, `uv tool update-shell` puts that directory on `PATH`.

Development means an editable install inside a uv-managed virtualenv:

```bash
uv venv                         # -> .venv (uv found CPython 3.14.4 here)
uv pip install -e .
```

**Read that one before running it.** `uv pip install` installs into the **active** `VIRTUAL_ENV` when
one is set — not necessarily the `.venv` beside you — so an editable install can quietly land in
whichever environment your shell happens to have activated, leaving this checkout uninstalled. Pin the
target explicitly, or work from a shell with nothing active:

```bash
uv pip install -e . --python .venv    # always this directory's .venv, whatever else is active
echo "$VIRTUAL_ENV"                   # empty output = nothing active (PowerShell: echo $env:VIRTUAL_ENV)
```

### Without uv

A stdlib virtualenv needs no extra tooling. POSIX shells:

```bash
git clone https://github.com/kirilan/eve-skills.git
cd eve-skills
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/eve-skills --version
```

PowerShell — the interpreter is `python`, and console scripts are `.exe` files under `Scripts`:

```powershell
git clone https://github.com/kirilan/eve-skills.git
cd eve-skills
python -m venv .venv
.\.venv\Scripts\pip install -e .
.\.venv\Scripts\eve-skills.exe --version
```

### No install at all

The package is plain stdlib Python, so from the checkout you can skip installation entirely and run
the module — the same `main()` the console script calls:

```bash
python -m eve_skills skills --json
```

Everything below assumes `eve-skills` is on `PATH`; substitute `python -m eve_skills` if it is not.

To build a wheel or source distribution instead of installing from the checkout — and for the
version-bump, artifact-inspection, clean-install and tag procedure — see
[RELEASE.md](RELEASE.md). Everything shipped here is GPL-3.0-only; the full text is in
[LICENSE](LICENSE) and travels inside both artifacts.

---

## Platform support

Linux and Windows are both supported: Python 3.11+, standard library only, no compiled dependency and
no platform-specific package. Commands, flags, output formats and exit codes are identical. Four things
really do differ, and each is handled rather than papered over — with a last note on how much of any of
it has actually been run.

**Locking.** A long-running `--watch` and a manual command can touch the same files, so every
read-modify-write holds an advisory lock — on POSIX with `fcntl.flock` (whole-file, blocking, dropped
by the kernel when the descriptor closes or the holder dies), on Windows with `msvcrt.locking` over
the first byte of that same lock file (`LK_NBLCK` is refused rather than queued, so it is polled every
20 ms, backing off to 250 ms until granted, then released with `LK_UNLCK`; the OS drops it when the
handle closes). The durability guarantees are the same on both: one writer at a time, nothing wedged
by a crashed watcher, and every durable write still a unique temporary plus an atomic rename — a crash
leaves the old file or the new one, never a mix. Two Windows details follow from its own contracts: a
rename fails outright while another process still has the destination open, so `os.replace` is retried
8 times across roughly 1.05 s before the original error propagates; and temporaries are opened
`O_BINARY`, because a text-mode handle would turn every `\n` written to `events.jsonl` into `\r\n`, and
its readers count lines. The lock is held on a zero-length sentinel file that nobody reads or writes —
precisely so the one real difference between the mechanisms (an `msvcrt` byte-range lock denies other
processes access to the locked region, while `flock` stays advisory even for its own file) can never
reach your data.

**File permissions.** On POSIX secret files are created `0600`, and `doctor` checks directories for a
group/other write bit. Windows has no such bits: a new file inherits the ACL of its directory, which
is normally your private user profile — that inheritance is what protects `tokens.json` there, not a
mode. So on Windows `doctor` reports `skip` for exactly the checks it cannot make (`path.config`,
`path.cache`, `path.data`, `path.state`, `config.file`, `permissions.tokens`) and says why, instead of
warning about the mode 666 every Windows `stat()` reports and offering a `chmod` that does not exist. A
skip is never a blocker: skips do not change the exit code, and everything else — including a corrupt
token store — still fails or warns as usual. To look at privacy yourself, use the folder's Security
properties; there is nothing in this tool to configure.

**Desktop notifications.** `--notify` shells out to `notify-send`, which no stock Windows installation
ships: treat it as a Linux feature. Everything else happens everywhere — an alert rings the terminal
bell, is printed, and is recorded in the event history either way. And rather than going quiet after a
flag that promised a ping, the watch says once per run:
`warning: desktop notifications need notify-send; events are still printed and recorded`.

**Watch redraw.** The dashboard clears the screen between frames when stdout is a TTY. A Windows
console ignores the escape sequence unless virtual-terminal processing is switched on, so the watcher
turns it on once per run; a console that refuses gets a plain 72-character rule line instead of escape
codes, with the timestamped header still separating the frames.

**How much of this has been run.** No Windows machine was involved in testing: every Windows branch —
the `msvcrt` backend, profile-folder path resolution, doctor's skips and its cmd-shaped hints, the
virtual-terminal fallback — is covered by tests that *force* the platform (a fake `msvcrt`, an injected
platform judgement), not by a live Windows run. See
[Testing and verification status](#testing-and-verification-status) for what that buys and what it does
not.

---

## EVE developer application

Authentication uses EVE SSO with your own application registration, so you need one before the
first login:

1. Open <https://developers.eveonline.com/applications> and create an application.
2. Set the **Callback URL** to exactly:

   ```text
   http://localhost:8635/callback
   ```

   Scheme, host, port and path must all match. `localhost` (not `127.0.0.1`), no trailing slash.
3. Tick the scopes you want to be able to use. The tool always requests the core three and adds
   only the optional ones you ask for:

   | Group | Scopes |
   |---|---|
   | Core (always requested) | `publicData`, `esi-skills.read_skills.v1`, `esi-skills.read_skillqueue.v1` |
   | `standings` | `esi-characters.read_standings.v1` |
   | `jobs` | `esi-industry.read_character_jobs.v1`, `esi-industry.read_corporation_jobs.v1` |
   | `assets` | `esi-assets.read_assets.v1`, `esi-assets.read_corporation_assets.v1` |
   | `location` | `esi-location.read_location.v1` |
   | `clones` | `esi-clones.read_clones.v1`, `esi-clones.read_implants.v1` |
   | `orders` | `esi-markets.read_character_orders.v1` |
   | `corp-orders` | `esi-markets.read_corporation_orders.v1`, `esi-characters.read_corporation_roles.v1` |
   | `structures` | `esi-universe.read_structures.v1` |

   Requesting a scope the application is not registered for makes SSO refuse the login.
4. Register it as a native/public application (no secret). Confidential registrations work too —
   pass `--client-secret`, and the tool sends it only in the HTTP Basic header, which is what CCP
   expects.

Supply the client ID once, in any of these ways (highest precedence first):

```bash
eve-skills login --client-id <your-client-id>          # stored in config.json after a successful login
export EVE_SKILLS_CLIENT_ID=<your-client-id>           # environment override, wins over config.json
# PowerShell equivalent of the same override:
#   $env:EVE_SKILLS_CLIENT_ID = "<your-client-id>"
# or edit config.json where your platform keeps it — ~/.config/eve-skills/ on POSIX,
# %LOCALAPPDATA%\eve-skills\config\ on Windows (see [where data lives](#where-data-lives-and-how-it-is-protected)):
#   {"client_id": "...", "user_agent": "..."}
```

`config.json` also accepts `user_agent`. The default User-Agent says `contact unset`; ESI etiquette
is to publish a contact address, so set it once you use the tool regularly.

---

## Login

### Local machine (browser opens automatically)

```bash
eve-skills login
```

The tool prints the authorization URL, tries to open your browser, and listens on loopback for the
redirect. With no `--port` it binds the first free port of 8635-8637; since only
`http://localhost:8635/callback` is registered in step 2 above, free **8635** before logging in, or
pick a fixed port you have also registered:

```bash
eve-skills login --port 8640                    # per run (register http://localhost:8640/callback)
export EVE_SKILLS_SSO_PORT=8640                 # same thing, persistent for this shell
```

The listener gives up after 5 minutes.

### Remote host (SSH) — `--manual`

On a remote host there is no browser and nothing listening on your workstation's localhost, so use
manual mode:

```bash
eve-skills login --manual
```

1. The tool prints the authorization URL and never opens a local listener at all.
2. Open that URL in the browser on your **workstation**, log in, approve consent.
3. EVE redirects to `http://localhost:8635/callback?code=...&state=...`. **The browser page failing
   to load ("can't reach this page" / connection refused) is expected.** The authorization code is
   already in the address bar; nothing needs to render.
4. Copy the **complete URL** from the address bar and paste it at the `Paste callback URL:` prompt.
   It must start with `http://localhost:8635/callback?` (if you passed `--port`, that port instead).
5. The tool verifies `state`, exchanges `code` for tokens over HTTPS, and stores them.

Security notes for manual login:

- The pasted URL contains a live authorization code plus the CSRF `state`. Treat it like a password:
  paste it only into this terminal prompt. Do not post it in chat, do not email it, do not commit
  it, do not paste it into a bug report or screenshot.
- Codes are single-use and short-lived; if you fumble the paste, re-run `login --manual` and start
  over rather than reusing an old URL.
- If you accidentally exposed a code before it was redeemed, just abandon that login attempt. If a
  *logged-in* machine may be compromised, run `eve-skills logout` there and remove the application's
  authorization from your EVE account (that server-side revocation is outside this tool; `logout`
  only deletes local token copies).

### Adding characters, extra consent, logging out

```bash
eve-skills login                          # another character: pick it in the browser
eve-skills login --scopes standings,jobs  # add consents for ONE character
eve-skills login --scopes all             # every optional consent, one character
eve-skills chars                          # who is stored, token time left, auto-refresh
eve-skills logout --char Somename         # drop one character; without --char: all of them
```

`login` itself takes no `--char`: which character gets stored is decided by the EVE account you sign
into in the browser, so run one `login` per character.

- Consent is **per character**. Refresh tokens inherit the scopes they were minted with, so adding a
  scope means re-running `login` and selecting that specific character in the browser. The tool
  never re-authenticates anyone implicitly and never bulk-grants.
- Valid `--scopes` values: `attributes`, `standings`, `jobs`, `assets`, `location`, `clones`,
  `orders`, `corp-orders`, `structures`, `all`. An unknown name is a hard error listing the choices.
- **Attributes need no scope of their own.** `attributes` (and exact `plan` costs) use
  `esi-skills.read_skills.v1`, which every login already requests; `login --attributes` is accepted
  for clarity and asks for nothing beyond the core skills consent.
- **Structure names are optional on top of `assets`.** `inventory` reads your holdings with the assets
  consent alone. Naming a player-owned citadel or engineering site is `/universe/structures`' job, and
  that needs `esi-universe.read_structures.v1`: without it those cells read `structure <id>` and the run
  prints the fix once (`eve-skills login --scopes structures`). A token that provably lacks the scope is
  never sent probing at all — every refusal costs ESI error-window budget that throttles the rest of the
  run.
- **Market prices need no consent at all** — `market` and `build-cost` read the public order books, so
  they work on a fresh install with nothing configured. Only *your own* orders are private: `orders` needs
  the `orders` consent, and `orders --corp` additionally needs `corp-orders` plus the in-game Accountant
  or Trader role (see [orders](#orders--open-and-closed-market-orders)).

---

## Commands

### `skills` — the main view

```bash
eve-skills                              # every stored character, text tables
eve-skills skills --char Somecharacter
eve-skills skills --filter omega        # all | alpha | omega (text table only)
eve-skills skills --sort sp             # name | level | sp
eve-skills skills --trained-only        # drop the queue section
eve-skills skills --week                # append SP gained over the last 7 days
eve-skills skills --json > skills.json
eve-skills skills --csv > skills.csv
```

The text block per character shows name/id, race, alpha clone grade, inferred clone state with its
confidence, up to three evidence lines and any warnings, total/unallocated SP, the training queue
(`TRAINING` / `queued` / `done` / `BLOCKED`), then the trained-skill table with per-skill alpha cap
and access column. Rows marked `*` finished training but ESI has not reflected them yet — they apply
on next in-game login. Never-trained prerequisite skills that ESI also lists are filtered out of the
table and out of CSV alike.

Output-format precedence: `--watch` wins over everything; otherwise `--csv` wins over `--json`.
`--filter`, `--sort` and `--week` shape the **text** table only — JSON always contains every row and
every field, CSV always emits all trained (or pending) rows in source order.

`skills --json` prints a single object for one character and a JSON array when several characters are
fetched.

CSV columns (`skills --csv`, one header row across all characters):

```text
character_id, character_name, skill_id, name, trained_level, active_level, sp,
alpha_cap, access, restricted, pending_completion, unknown_data
```

### `summary` — fleet-wide one-liner

```bash
eve-skills summary
```

One row per fetched character (clone state + confidence, total SP, queue items, time left on the
currently training item) plus a `TOTAL` row. Characters that fail to fetch are reported as
`warning: skipped ...` on stderr and do not abort the command.

### `attributes`

```bash
eve-skills attributes --char Somecharacter
```

PER/INT/MEM/CHR/WIL, remaps available, last remap date, and accelerator days remaining when nonzero.
No extra consent is needed; a character whose stored consent somehow lacks the skills scope gets a
hint line instead of data.

### `standings`, `jobs`, `travel`, `implants`

```bash
eve-skills standings --csv > standings.csv
eve-skills jobs                       # personal jobs
eve-skills jobs --corp --completed    # corp jobs incl. finished/cancelled
eve-skills travel                     # current location, home, jump clones + their implants
eve-skills implants                   # implants in the active clone (one row per fitted instance)
```

All four accept `--char` and `--csv`. In CSV mode any consent hint is written to **stderr** so stdout
stays machine-readable. Corporation variants (`jobs --corp`, `inventory --corp`) additionally need
the matching director / Account-Manager role on that character; without it ESI refuses and the tool
prints a per-character warning rather than failing the whole command.

### `inventory` — what you own, where it is, what it is worth

```bash
eve-skills login --scopes assets,structures   # once, per character, in the browser
eve-skills inventory                          # summary grouped by where the items are
eve-skills inventory --by category            # the same numbers, turned the other way round
eve-skills inventory --items                  # one row per item, most valuable first
eve-skills inventory --value-at jita          # value at the richest standing buy order there
eve-skills inventory --value-at "The Forge"   # …or across a whole region, not one station
eve-skills inventory --corp                   # corporation assets (director / Account-Manager role)
eve-skills inventory --json > holdings.json
eve-skills inventory --csv > holdings.csv     # footnotes go to stderr, so the pipe stays clean
```

ESI's asset rows are numbers — a `type_id`, a `location_id`, a `location_type` — and nothing else. This
command turns them into what each item is (name, group, category), where it actually sits, and what it
is worth on one of two labelled bases. Each owner gets a heading (`<name> (<N> asset rows)`), one table,
and a grand total under it; a character with nothing in their holdings gets `(inventory empty)` instead
of an invented zero.

**Three views.** The columns, exactly as printed:

```text
--by location (default)   location | category | types | units | value
--by category             category | location | types | units | value
--items                   item | group | category | qty | location | unit price | value
```

- **`--by location`** (the default) opens a section per root place — the station, player structure or
  open system that everything in it folds up into — and breaks that down by category. Each section ends
  with a `subtotal <place>` row; the table ends with the grand total.
- **`--by category`** is the same aggregation transposed: sections by category with a
  `subtotal <category>` row each, rows by place. The location cell carries the full nested path here, so
  a row reads on its own whichever way round the table is turned — and the TOTAL line comes out identical,
  because it is the same money.
- **`--items`** drops the grouping: one row per asset, most valuable first, with every unpriced row
  behind every priced one and names breaking the ties.

**Where an item really is.** A `location_id` can name a station, somebody's citadel, open space, or
another item — so the view walks the parent chain and prints it root-first:

```text
Jita - Mradd > My Freighter > Cargo Hold
```

That is a module in a container in a ship docked at a station, and each view reads a different slice of
it. The default `location` column and its section heading are the **first** cell — the place everything
under it folds into, which is why an item two levels inside a docked ship counts in that station's
subtotal. The location column of `--items` and of `--by category` is the **whole chain**. CSV's legacy
`location_name` stays the **last** cell — the container the item is physically in — with the full chain
beside it in `location_path`. Kinds are `station`, `structure`, `system` (loose in space), `container`,
`ship`, `other`. A structure this token may not see keeps a stable `structure <id>` cell and the run says
so once with the command that fixes it — a label that names the id, never a bare number.

**Names its owner chose.** A player-named singleton shows both halves — what they call it and what it is:

```text
Morning Bell (Rifter)
```

ESI answers `assets/names` with the literal string `"None"` for an item nobody ever named (measured: 18
of one character's 23 singleton rows), so that placeholder is dropped and the type name wins. Otherwise
most of a hangar would read `None (<Type>)`; only a player who really typed *None* as a ship name gives
anything up by that rule.

**`--json`** is one document: `generated`, `owner_kind`, `grouped_by`, the whole `value_basis` (key,
label, short label, scope, requests, priced and unpriced types, failed books, freshness, the alternative
listing figure, cached figures), `hints`, `warnings`, and per character its `character_id`, `name`,
`asset_rows`, `totals`, `groups` (the grouped view, subtotals included) and `items`. Every item carries
ids and names together — `item_id`, `type_id` + `type_name` + `name` (the display name), `custom_name`,
`group_name`, `category_name`, `quantity`, `singleton`, `flag`, `location_id`, `location_kind`,
`unit_price`, `value`, and `location_path` as objects with `id` / `name` / `kind` — so a reader never has
to resolve anything afterwards, or guess which of two names it is looking at. Prose stays off stdout:
hints and warnings are fields here, not lines.

**`--csv`** is always per-item and carries 19 columns. The nine this command has always published keep
their exact positions and meanings, and everything new is appended behind them — so a spreadsheet that
read the old header still reads the same thing out of the same column:

```text
character,item_id,type_id,item_name,quantity,singleton,flag,location_id,location_name,
group_name,category_name,custom_name,location_path,location_kind,price_basis,price_scope,unit_price,value,unpriced_types
```

`item_name` still means the name of `type_id`; a player's own label gets its own `custom_name` column
rather than quietly replacing it. `unpriced_types` is that owner's count of types this basis could not
price, repeated on each of their rows. An unpriced row leaves `unit_price` and `value` empty rather than
writing `0`.

**Two bases, because they answer different questions.** The money column always says which one produced
it.

- **Default — `ESI reference`.** One `GET /markets/prices` request prices every distinct type held:
  *"ESI's published reference price - a figure CCP publishes about an item, not an order anybody will
  fill"*. It uses `average_price`, falling back to CCP's industry `adjusted_price` only for rows with no
  average. **A reference price is not a quote**: nothing can be bought or sold at it, and it does not
  move when the market moves.
- **`--value-at HUB|REGION` — `max buy @ <scope>`.** *"the richest standing buy order at {scope} - what
  dumping the holding there pays right now"*. A hub (`jita`, `amarr`, `dodixie`, `rens`, `hek`) reads
  that station's orders only, because money you cannot reach is not a valuation; naming a region (exact
  name or id) widens past it. This costs one order-book request per distinct type the cache cannot
  answer, and the scope is resolved before any asset page is fetched — a mistyped hub should cost one
  lookup, not a haul. **`max buy` is what dumping pays, not what listing would raise**: from the very
  same rows the run also prints the free alternative — `listing the same holdings at <scope>'s cheapest
  standing ask would raise <X> ISK over <N> types` — so both figures are on screen and neither gets
  mistaken for the other.

**Unpriced is not worthless.** `-` in a money cell means this basis has no figure for that type; `0.00`
means ESI published zero, which is a price. Unpriced types are excluded from every total — row, subtotal
and grand total — counted out loud (`priced 4 of 5 distinct types held (…)`) and named there
(`no price on this basis, excluded from every total above (1): <Type Name>`). A book that never answered
is a third statement: `N of those books did not answer; their types are counted as unpriced above, not as
worthless`. The grand total therefore quotes only the units it actually priced, with the basis inside its
own label (numbers below are illustrative):

```text
TOTAL (max buy @ Jita 4-4): 1,234,567.00 ISK over 987 units of 42 distinct types; 3 more types held, none priced
```

When nothing on this basis has a figure it says `nothing priced on this basis (N distinct types held)`
rather than printing `0`.

**What it costs.** Two fan-outs hide behind this command, and telling them apart is the difference
between a slow tool and a broken one.

- **The type catalogue — paid once per new type, ever.** The first time this machine meets a type it
  fetches `/universe/types/{id}`, then one request per distinct group those types name, then one per
  distinct category those groups name. Measured on a real holding of 518 distinct types: **518 + 168 +
  18 requests ≈ 125 s cold**, and the same holding warm in **9 requests ≈ 3 s**. Types, groups,
  categories, station and system names never change, so they come off disk forever — a newly met type in
  an already-known group costs exactly one request. What is re-read every run is the personal half (the
  asset rows themselves, custom item names, token-visible structures), because those change the moment a
  ship is renamed or a citadel repacked.
- **`--value-at` — one order book per distinct type.** Measured on that same 518-type holding: cold
  **100.6 s / 518 requests**; an immediate repeat **14.1 s / 63 requests** (455 types answered from
  disk); `--items` straight after that **4.0 s / 18 requests** (500 cached). Books go through eight
  workers at roughly five a second, so the wait scales with how many distinct *types* you hold, not with
  how many items.

**The wait says what it is buying.** Before a book fan-out starts, stderr gets counts rather than a
progress bar:

```text
pricing 518 distinct types held: 518 order books to read, one per type; about 1m 38s at this size
```

with `(455 already priced from the last run)` inserted when part of it is cached — and, when nothing
needs fetching at all, `pricing 518 distinct types held: every figure already in the local quote cache,
so no order book is read`. The duration is quoted only above ten seconds: below that the wait needs no
explaining, and a notice about nothing teaches you to stop reading notices. `--json` drops it — those
counts are already fields in `value_basis` — while `--csv` still prints it on stderr, where every other
footnote for that mode lands too.

**The quote cache.** `--value-at` writes `quotes.json` in the cache directory: for each (region,
station/system filter, type) just the reduction of that book — `min_sell`, `max_buy` — plus the
response's own `Last-Modified` and `Expires`. Never the order rows themselves; about 85 KB for 518 types.
An entry is served only while its own stated `Expires` is still in the future, so a warm run never prints
a figure ESI has already disowned — and a figure reused from disk keeps the stamp ESI gave it: **a printed
total is stamped with the oldest contributing `Last-Modified`, cached or fetched**, so it can never be
described as fresher than its stalest input. The default reference basis does not go through this cache:
it is one document for the whole cluster, left to the transport's own `Expires`/ETag handling. Writes
merge under a lock and drop expired records, so two characters valued from two shells cannot lose each
other's figures, and a half-written or hand-edited file costs a refetch rather than a wrong price.

### `orders` — open and closed market orders

```bash
eve-skills login --scopes orders,corp-orders    # once, per character, in the browser
eve-skills orders                               # every stored character's open orders
eve-skills orders --char Somecharacter --sell   # one character, sells only
eve-skills orders --type Tritanium              # one type (exact name or numeric id)
eve-skills orders --corp                        # corporation orders instead of personal ones
eve-skills orders --closed --limit 200          # ESI's ~90-day history, newest issued first
eve-skills orders --watch 1                     # poll every minute, announce what changes
eve-skills orders --csv > orders.csv
```

Two consents, because two people own the data: `orders` (`esi-markets.read_character_orders.v1`) for
your own books, and `corp-orders` for the corporation's — that one also asks ESI for your role list,
and ESI still requires the in-game **Accountant** or **Trader** role. A character without the consent
is a hint, not an error (`Ada Vane: no orders consent - run: eve-skills login --scopes orders`), and a
403 from a corporation book names which of the two causes it believes it is — because the fixes are
opposite: re-login for consent, ask a director for the role.

Open orders show owner, type, side, price, `remaining/total`, filled count, station, region, issued
time, time left and escrow. The footer totals the two sides separately — `sell book … ISK` is what the
remaining sell volume would raise, `buy escrow … ISK` is what your unfilled buys have already been
charged into escrow — and says `escrow reported for K` when only K of the N buys told ESI that number,
because the escrow field is optional and a total that quietly skipped the rest would be a lie. A
personal book can hold an order funded from the corporation wallet, and the side column says
`sell (corp)` for it — under `--corp` every row belongs to the corporation, so the marker is dropped and
the table instead gains `division` (wallet division) and `issued by`. Corporation books are fetched once
per corporation even when several stored characters work there, so colleagues' orders are never counted
twice. `--closed` switches to ESI's history: price, derived state,
`filled/total`, station, region, issued and expires — with two footnotes about what history cannot say
(see [Limitations](#limitations-you-should-know)).

### `market` — live prices, spread and volume

```bash
eve-skills market Tritanium                 # Jita 4-4, station level: the default scope
eve-skills market Tritanium --hub amarr     # jita | amarr | dodixie | rens | hek (repeatable)
eve-skills market Tritanium --region "The Forge" --region 10000043   # region name or id, repeatable
eve-skills market Tritanium 34 --hub jita   # several types in one run; ids work as names do
eve-skills market Tritanium --global        # add the best price across every market region
eve-skills market Tritanium --history 30    # append ESI's daily traded volume
eve-skills market Tritanium --json          # / --csv for scripting
```

No login and no consent: the order book is public, so this works on a machine that has never seen
SSO. Each requested scope is one row — `min sell`, `max buy`, `spread`, `margin %`, listed sell/buy
volume, order counts, and the station holding the best sell and best buy order — and every row gets
its own freshness line under the table, taken from ESI's `Last-Modified` for that book:

```text
$ eve-skills market Tritanium --hub jita --hub amarr
Tritanium (id 34)
scope               min sell  max buy  spread  margin %  sell vol        buy vol         sells  buys  …
------------------  --------  -------  ------  --------  --------------  --------------  -----  ----  …
Jita 4-4 (station)  3.94      3.79     0.15    3.96      7,532,892,507   8,628,142,550   48     36    …
Amarr (station)     3.43      3.33     0.10    3.00      1,688,312,309   1,542,971,953   25     16    …
  Jita 4-4 (station): as of 13:31:08Z (3m 40s ago; ESI refreshes the book every 5 min)
  Amarr (station): as of 13:31:39Z (3m 09s ago; ESI refreshes the book every 5 min)
```

One line per scope rather than one stamp for the whole table because the books really are different
ages: ESI regenerates each regional book at most every five minutes, and scopes are fetched
separately. `--global` scans every region with a market (k-space plus Pochven — 70 regions today),
adds a `global (70 regions)` row whose freshness line is the *oldest* book that fed it, and prints
`warning: … N of M regions did not answer; their orders are missing from the numbers above` when part
of the cluster stayed silent. `--history DAYS` appends `traded/day*` and `traded total*`, which come
from a different ESI document with real caveats — hence the asterisk and the footnote:

```text
* ESI traded volume is daily and one day behind, and only exists per region: a hub row shows its
  region's trades, the global row shows none.
```

Prices come from ESI alone by design. Third-party aggregators (Fuzzwork, EVERef and similar) are not
consulted: they are other people's databases of these same books, they add their own staleness and
availability on top of ESI's, and this tool has no way to be told when one of them is wrong.

An empty row says the books this run read had nothing in them, which is a much narrower statement than
"nobody is trading this" — so the footnote under it is chosen by what was actually asked, not by the
fact that nothing came back:

| What the run covered | What the footnote does |
|---|---|
| Stations or systems only (the default, `--hub`) | Says those books are empty and nothing else, then names the wider book still unasked in a form you can paste: `try --region "Heimatar"` |
| Whole regions, but not all of them (`--region`) | Says those regions are empty and nothing else; `--global` is what is left to ask |
| Every market region (`--global`) | Nothing wider does exist — ESI has no endpoint above the regional books — and adds that this is a statement about coverage, not an explanation of why |
| A type measured empty in all 70 regions (PLEX) | The one case with a measured cause: it trades on the account-wide vault market, which belongs to no region's book |

```text
$ eve-skills market "Mystic XL" --hub rens
Mystic XL (id 92952)
scope           min sell  max buy  spread  margin %  sell vol  buy vol  sells  buys  …
--------------  --------  -------  ------  --------  --------  -------  -----  ----  …
Rens (station)  -         -        -       -         0         0        0      0     …
  Rens (station): as of 17:20:00Z (2m 24s ago; ESI refreshes the book every 5 min)
  No orders in any requested scope, which is a statement about those books and nothing else: an
  item nobody stocks at one station trades freely at the next. The wider book is still unasked -
  try --region "Heimatar", which reads the whole regional book ESI publishes.
```

Only the bottom two rows of that table have earned the right to quote a number, because they are the
two cases where no wider order book exists to ask for. The others get the wider question instead of a
figure: an item absent from one station is absent from one station, and pricing it from there is how a
reader ends up believing a thinly traded module has no market at all.

```text
$ eve-skills market PLEX --hub jita
PLEX (id 44992)
scope               min sell  max buy  spread  margin %  sell vol  buy vol  sells  buys  …
------------------  --------  -------  ------  --------  --------  -------  -----  ----  …
Jita 4-4 (station)  -         -        -       -         0         0        0      0     …
  Jita 4-4 (station): as of 17:06:02Z (2m 08s ago; ESI refreshes the book every 5 min)
  No orders in any requested scope: ESI publishes order books per region only, and no regional
  book can show this type - it trades on the account-wide vault market instead.
  There is no global order-book endpoint above them, so no wider book was left to ask.
  ESI's published reference for this type: average 4,574,918.36 ISK, industry adjusted 0.00 ISK
  freshness: as of 16:56:10Z, 12m 00s ago; ESI stamps this document on its own schedule, not with the order books
  A published figure, not a bid or an ask: nothing can be bought or sold at it.
```

Those two numbers are CCP's industry `adjusted_price` and a rolling `average_price` from
`/markets/prices`: never a bid or an ask, never folded into `min sell` / `max buy`, and stamped with
their own age rather than the order book's — ESI's stamp for this document moves on its own schedule
(an hour ahead of its `Last-Modified` when measured), which is not the five minutes a book claims. A
type that document has no row for either says exactly that rather than inventing a figure, and a row
whose `adjusted_price` really is `0.0` prints `0.00`, not a dash, because zero is what CCP published.
The document covers every priced type in one request over a megabyte, so it is read only when one of
those two footnotes is going to quote it: wanting `--json` or `--csv` is no longer a reason to buy it,
and a run whose books had orders — or whose empty book still has a wider scope to try — sends no
request for it at all. `--json` therefore carries a `reference` object for every type tagged
`"kind": "esi_published_reference"`, and `null` means either that this run had no reason to ask or that
ESI's document has no row; `--csv` appends `reference_average_price`, `reference_adjusted_price`,
`reference_last_modified` and `reference_age_seconds` at the end — empty cells for both of those cases
— so a header a script already reads keeps meaning. The text output is where the two are told apart,
because there the footnote says which of them it was.

### `build-cost` — what manufacturing an item costs right now

```bash
eve-skills build-cost Hound                       # one install, ME 0 top / ME 10 components, shopping at Jita 4-4
eve-skills build-cost Hound --runs 5 --me 10      # five installs on a researched blueprint
eve-skills build-cost Hound --component-me 0      # the component blueprints are unresearched too
eve-skills build-cost "Fernite Carbide"           # a reaction: 10,000 units a run, no ME or TE
eve-skills build-cost Hound --build "Plasma Thruster"    # force one component to be built anyway
eve-skills build-cost Hound --buy-all             # never run a component job; buy everything
eve-skills build-cost Hound --hub amarr           # shop at another trade hub's station
eve-skills build-cost Hound --system Amarr        # bill the install in Amarr, still shop at Jita
eve-skills build-cost Hound "Plasma Thruster"     # several products, off one set of order books
```

No login and no consent: the recipe comes from the local SDE snapshot — shipped in the package and
refreshed by `update-data` (the bundled one is build 3494416, 4952 blueprints covering 4943 distinct
products) — and the prices come from public order books, so this runs on a machine that has never seen
SSO. Nothing about the recipe is estimated: it is CCP's own material list for the blueprint that makes
the type, with that blueprint's own material efficiency applied to the quantities.

```text
$ eve-skills build-cost Hound
Hound (id 12034) - 1 unit from 1 run, at ME 0 / TE 0, components at ME 10, blueprint 12035
material                               qty  buy/u       build/u     source  cost          surplus
-------------------------------------  ---  ----------  ----------  ------  ------------  -------
Fernite Carbide Composite Armor Plate  300  5,168.00    5,505.37    buy     1,550,400.00  0
Nanomechanical Microprocessor          180  45,240.00   43,505.85   build   7,831,053.22  0
Ladar Sensor Cluster                   60   18,410.00   18,981.93   buy     1,104,600.00  0
Electrolytic Capacitor Unit            60   43,350.00   43,135.18   build   2,588,110.90  0
Morphite                               38   17,790.00   -           buy     676,020.00    0
Construction Blocks                    30   10,200.00   -           buy     306,000.00    0
Plasma Thruster                        30   34,970.00   30,020.23   build   900,606.79    0
Deflection Shield Emitter              15   30,690.00   30,024.98   build   450,374.65    0
Nuclear Reactor Unit                   6    106,900.00  98,169.72   build   589,018.35    0
R.A.M.- Starship Tech                  3    799.00      22,610.21   buy     2,397.00      0
Breacher                               1    509,400.00  485,204.50  build   485,204.50    0
  A component job runs in whole runs, so the build column charges every run needed to
  cover the quantity above: a blueprint that yields more than the recipe wants leaves
  real surplus behind - product you own and could sell, not waste.
totals:
  material cost   16,483,785.40 ISK
  EIV             12,165,171.20 ISK  (base quantities x ESI adjusted price; ME does not reduce it)
  job cost        2,613,078.77 ISK  = EIV x (0.1723 cost index + 0.0025 facility tax + 0.0400 SCC surcharge)
  total           19,096,864.18 ISK
  cost per unit   19,096,864.18 ISK
  job time        1d 09h
buy instead: cheapest ask 15,980,000.00 ISK, richest bid 15,020,000.00 ISK at Jita 4-4 (station)
  buying is cheaper by 3,116,864.18 ISK for 1 unit (build 19,096,864.18 vs buy 15,980,000.00)
```

`buy/u` is the cheapest standing ask in the scope this run shops; `build/u` is what one unit of that
material costs if you run *its* blueprint instead — its own materials at their asks plus its own install
fee. `source` says which of the two won, per material: the cheapest option is taken row by row, and a
dash under `build/u` means there is nothing to compare, either because no local blueprint makes that
material or because building it cannot be priced. So the table is not a list of what you must buy — it
is the build-or-buy decision made for every input, which is the part worth automating.

Two research levels are named rather than one, because one level described a build nobody actually
makes. `--me` is the level of the blueprint being run; the jobs feeding it run at `--component-me`,
which defaults to ME 10 — component blueprints are ordinarily owned BPOs their holder has had long
enough to take to the cap, while the top blueprint of a T2 item so often is an invented copy that
carries no research at all. Guessing the pessimistic end for components was the single largest
distortion this command made, because ME is charged against every material of every component and not
only against the top job's: on the run above, `--component-me 0` costs 19,509,454.53 against
19,096,864.18. Researching the *top* blueprint stays a separate knob — `--me 10` took the same item to
17,604,461.25 and flipped more rows from buying to building. Both figures were re-measured minutes
after the table above, so neither matches it to the penny; order books move between runs.

The three money lines are deliberately kept apart because they answer different questions. `material
cost` is the sum of the chosen options. `EIV` is CCP's billable value — base quantities times ESI's
adjusted price — and ME does not reduce it, which is why a researched blueprint cuts the material bill
and leaves the install fee alone. `job cost` prints its own arithmetic instead of one opaque number:
the system's industry cost index, plus the facility tax (`--facility-tax`, 0.25% for an NPC station),
plus the 4% SCC surcharge. Each has a different knob — `--system` picks the index, `--facility-tax`
picks the tax, and the surcharge is CCP policy (see [Limitations](#limitations-you-should-know)).

Substitution goes exactly one level deep, and that boundary is a request-budget decision, not an
oversight: each component's own materials are priced from the books this run already opened, so the
second level is free, while a third would need fresh books for every material of every component.
Measured over all 4943 products in the snapshot, one product needs a median of 8 distinct types priced,
p90 19, and 71 at the worst (Vanquisher) — multiplying that out is how a cost estimate stops being
cheap enough to ask casually. The cold run above spent 30 requests: 26 order books, one
`/industry/systems` for the cost index, one `/markets/prices`, and two name lookups. A second run inside
the quote-cache window reads no books at all, on the same rule `inventory --value-at` uses — ESI's own
`Expires` vouches for a cached figure or it is refetched. Asking for several products together is
cheaper per product because a material two of them share is read once.

Forcing is allowed and is not free, which the output shows rather than hides. On this item the mixed
default (19,096,864.18) beats `--buy-all` at 19,656,945.77 and `--build-all` at 19,298,269.07; forcing
`--build "R.A.M.- Starship Tech"` lifts the total to 19,162,681.81 and leaves 97 surplus units of that
component behind. Surplus is real product, not waste — a component job runs in whole runs, so a
blueprint yielding more than the recipe wants leaves something you can sell, and the `surplus` column
says how much. Reactions are their own activity: `build-cost "Fernite Carbide"` runs 10,000 units per
run, has no ME or TE of its own to research — so `--me` changes nothing there while its components
still run at `--component-me`, and the heading says both — and is billed with that system's *reaction*
index rather than manufacturing's. Five products in the snapshot are made by more than one blueprint;
the lowest blueprint id is used and the others are reported as alternatives rather than silently
averaged.

`--system` bills the install and nothing else — it does not move the shopping, so `--system Amarr` with
the default scope means buy at Jita, install in Amarr. That distinction matters because component jobs
are billed at the same index as the top job, so the system you name changes the material line too.
`--region` needs `--system` alongside it and refuses without one: a region has no single cost index, and
picking a system for you would be inventing a number. A named system whose industry document publishes
no manufacturing index is refused as well — with the note that only the tax and surcharge would apply —
after the books have been read, since nothing else about the run was wrong.

A material with neither an ask in scope nor a published price is reported as `-`, kept out of every
total rather than counted as free, and takes the per-unit figure down with it: `cost per unit` prints a
dash with the reason beside it, because a per-unit number with a hole in it is not something to quote to
another person. The notes under the table name the type and say that the install fee understates its
share, since EIV cannot include what has no adjusted price either.

Refusals exit 1 and ask for nothing: no local blueprint makes the requested type (the message names
`eve-skills update-data`, which is the only thing that can fix it), `--me` or `--component-me` outside
0..10, `--te` outside 0..20, `--build-all` together with `--buy-all`, and `--region` without `--system`.
Machine-readable output keeps the decision rather than only its winner: every material row carries a
`build` object with the blueprint id, runs, units, surplus, its own `material_cost` / `job_cost` /
`total` and per-unit cost — even on a row that was bought, so a script can see the option that lost and
by how much — or `null` where no blueprint makes it. Note that `build.unit` is the cost per unit
*produced* by that component job while the table prints cost per unit *needed*, which differ whenever a
run overshoots the recipe. Each product object reports the levels its money was priced at as `me`,
`te` and `component_me`, and `--csv` carries the same three columns in that order. Unpriced types appear
under `unpriced`, and `--csv` writes one row per material to stdout with every note on stderr, so a
header a script already reads keeps meaning.

### `plan` — training plan (one character)

```bash
eve-skills plan --char Somecharacter "Astrogeology:5" "Nanite Operation"
eve-skills plan --char Somecharacter --rate 3600 "Local Industry:4"
```

Target syntax is `SKILL[:LEVEL]`, level defaulting to L5. Names resolve against the **full SDE
skill catalog** installed by `update-data` — exact match first, then a unique substring; an
unknown or ambiguous name is an error listing what it found, and a missing catalog tells you to
run `eve-skills update-data`. Skills the character has never trained are planned just fine. A
repeated target means its deepest level.

What the plan does:

- **Prerequisites expand automatically**, recursively, at the highest level any target needs
  them; a shared prerequisite appears exactly once, and row order guarantees every skill trains
  after its prerequisites (foundations first, stable ties by name). The `why` column says whether
  a row was requested or which skills it unlocks.
- **Costs are rank-based and exact**: cumulative SP at level L is `round(250 × rank ×
  2^(2.5·(L−1)))`, the canonical CCP table. Attributes never change the SP amount, only how fast
  it accrues — there is no attribute-sum estimate any more.
- **Start levels are queue-aware**: an existing queue entry that already raises a skill sets the
  item's start level (marked `*` in `now`), and a target or prerequisite already covered by the
  trained or scheduled level costs nothing and is reported as covered.
- New items are scheduled after the existing queue drains; that backlog finish time is printed.
- The rate comes from `--rate <SP/hour>` if given, else a live `TRAINING` queue item (ground
  truth — the measurement already includes implants, remaps, clone state and the attributes of
  the skill being trained), else the slope of local SP history over 7 days. Flat or
  extraction-dipped history is deliberately rejected as a rate and the command tells you to pass
  `--rate`.
- Notes flag targets above the character's alpha cap, omega-only skills, and skills CCP no longer
  publishes.

Remaining caveats, stated by the tool itself: one calibrated rate prices every row, so plans
spanning several primary/secondary attribute pairs are estimates for rows driven by other
attributes (the output names the pairs involved); remaps or implant changes during training shift
real time; implants are never modeled into future levels. A prerequisite the local catalog has no
row for, or a cyclic prerequisite chain, is an error naming the skill or the cycle.

### `extract` — Skill Extractor math (one character)

```bash
eve-skills extract --char Somecharacter
```

Reports allocated vs unallocated SP, how many 500k SP extractors the character can run while keeping
the 5,000,000 trained-SP floor (5,500,000 allocated SP minimum to extract at all), the approximate
re-training days per extractor at the calibrated rate, and what re-injecting here would yield given
the injector tiers (500k below 5M total SP, 400k to 50M, 300k to 80M, 150k above). Queued/training SP
is not extractable. These are dated constants verified against CCP on `2026-09-05`; after ~180 days
the command prints a staleness warning instead of the "rules per CCP" line.

`plan` and `extract` require `--char` when more than one character is stored — they are single-character
operations by design.

### `watch` — live terminal dashboard

```bash
eve-skills skills --watch          # refresh every 5 minutes (training + your market orders)
eve-skills skills --watch 1        # every minute (minimum 1)
eve-skills skills --watch 10 --notify
eve-skills skills --watch --full   # full per-character views instead of the compact table
eve-skills skills --watch --no-orders   # training only: leave the order books alone
eve-skills orders --watch 1        # the order books on their own, every minute
eve-skills orders --watch --corp   # corporation orders as well (needs corp-orders consent)
```

By default watch draws a compact status table for all stored characters — clone state, queue
length with finished-item count, what is training now and its time left (or `blocked (no
schedule)`), total SP, and fetch freshness. It redraws on the timer (clearing the screen only
when stdout is a TTY — on a Windows console only after virtual-terminal processing has been switched
on, with a plain rule line as the fallback for one that refuses it; see
[Platform support](#platform-support)). `--full` keeps the full `skills` view instead. Ctrl-C stops it
and exits 130.

A character whose fetch fails keeps its last-known row marked `<age> stale`, with the error
printed under the table — healthy data is never dropped because one sibling went quiet; a
character never fetched shows `no data yet`. A cycle where no character data could be fetched at
all prints `warning: no character data this cycle - retrying next poll` and keeps the previous
training state, so a transient ESI outage cannot fake a "finished" event.

Alerts fire **exactly once**: every transition a watcher witnesses — training finished, queue
emptied, an order filled, expired or cancelled — is claimed against persisted state under a lock,
so each event is announced once across polls, restarts and concurrent watchers — never repeated
per poll. Announcements ring the terminal bell; `--notify` additionally calls `notify-send` when that
binary exists, which in practice means Linux — Windows ships nothing by that name, so the watch prints
one explanation per run rather than going quiet, and the bell, the printed line and the recorded event
are unchanged. A missing, hanging or failing notify-send never kills an overnight watch. The history
lives in the state directory (`$XDG_STATE_HOME/eve-skills`, `%LOCALAPPDATA%\eve-skills\state` on
Windows) and survives restarts — read it with
[events](#events--recorded-watch-history). There is no mail/push/webhook delivery.

`skills --watch` polls the order books too — the run you already leave open overnight is the one that
should notice a fill — and `--no-orders` opts out. Order state lives beside the training state, keyed
by **owner** (`char:<character_id>`, or `corp:<corporation_id>` for a corporation book), because the
same corporation can be watched through any colleague's token. Two rules follow from that:

- **The first poll of an owner announces nothing.** ESI's ~90-day order history is ingested silently,
  so starting a watch does not shout months of old closures as if they had just happened. They are
  still recorded, and `events` marks them `[history]`.
- **A fill, an expiry or a cancellation happens once, ever.** Ids already settled are remembered, so a
  history row surfacing in a later poll — or a second watcher on the same character — adds neither a
  row nor noise. An order that leaves the live book without a matching history row waits two days for
  ESI's backlog to catch up, then is recorded as `order_closed`; if it reappears meanwhile it was a
  cache flicker and nothing is said. A cycle whose history fetch failed freezes conclusions the same
  way: the open book is refreshed and a disappearance still starts its wait, but nothing is declared
  closed on a cycle that could not check.

### `events` — recorded watch history

```bash
eve-skills events                      # most recent 50 events, newest first
eve-skills events --char Somecharacter --limit 200
eve-skills events --kind order_filled  # repeatable: one kind per flag
eve-skills events --owner "corp:98356123"      # one owner's order events, by exact key…
eve-skills events --owner ledger               # …or by part of its name, case-insensitive
eve-skills events --json
eve-skills events --csv > events.csv
```

A read-only view of the alert history the watchers record: it never fetches and never writes. Kinds
are `training_finished`, `queue_empty`, `order_filled`, `order_expired`, `order_cancelled` and
`order_closed`; `--kind` filters to any subset and composes with the other filters.

`--char` accepts a stored name or id; a bare numeric id also finds events for characters already
logged out. Order events belong to an *owner*, not necessarily to a character — a corporation
announcement deliberately has no `character_id`, because it was read through one colleague's token
but is nobody's personal order — so `--char` cannot reach those rows and `--owner` exists for them:
exact owner key (`char:90000001`, `corp:98000001`) or a case-insensitive fragment of the owner name.
Training events have no owner, so an `--owner` filter never returns one.

Unreadable history lines are skipped with a
`warning: skipped N unreadable event history line(s)` on stderr. When nothing has been recorded,
the text output says so honestly instead of printing an empty table, `--json` prints `[]` and
`--csv` prints just the header row — one header serving both kinds, with empty cells wherever a
training row has no order fields (and vice versa):

```text
id,ts,time_utc,kind,character_id,character_name,skill_id,skill_name,finished_level,finish_date,order_id,owner_key,owner_name,type_id,type_name,is_buy,price,volume_total,volume_remain,filled,region_id,location_id,issued,expires,wallet_division,issued_by,backfill,ts_estimated
```

`backfill=1` means the event was read out of ESI's history rather than witnessed between two polls;
`ts_estimated=1` means ESI could not say when the order closed, so the timestamp is the best bound it
has (never later than the order's own expiry). The text view prints those two as `[history]` and
`[time estimated]` on the affected lines.

### `update-data` — alpha caps, skill catalog and blueprint recipes from the official SDE

```bash
eve-skills update-data              # latest build (~100 MB download)
eve-skills update-data --build 3494416   # example: pin a known specific build
```

Downloads the official JSONL SDE zip from `developers.eveonline.com`, extracts clone grades, bloodline
races, the full skill catalog (name, rank, attributes and prerequisites for every catalogued skill) and
the material list of every blueprint — manufacturing runs and reactions alike, keyed by the product each
one makes — and atomically replaces four files in the data directory
(`$XDG_DATA_HOME/eve-skills`, `%LOCALAPPDATA%\eve-skills\data` on Windows). That user copy takes
precedence over the snapshot shipped in the package, so you can refresh caps, the catalog and the recipes
without touching the checkout. `plan` is built on this catalog — without one it refuses with `no local
skill catalog - run: eve-skills update-data` — and `build-cost` is built on the recipes, which is why a
type that nothing makes locally ends by naming `update-data` instead of printing a table of dashes. The
whole download runs under `update.lock`, so two concurrent runs cannot both pull ~100 MB and interleave
builds, and each file is replaced atomically. `skills` warns when the local snapshot is more than 90 days
old (the age line also names the SDE build in use).

### `doctor` — installation diagnostics

```bash
eve-skills doctor                 # offline checks
eve-skills doctor --json          # same report, machine-readable
eve-skills doctor --network       # also probe EVE SSO discovery and public ESI endpoints
eve-skills doctor --timeout 5     # per-request timeout for the probes (default 10 s)
```

Read-only by construction: `doctor` never writes — no token refresh, no endpoint caching, no
migration, no directory creation — so it reports on your install exactly as the next real command
will find it. Offline checks cover the package, the Python version and the OS the report was produced
on (`versions.platform` — a pasted report may be read on a different machine than wrote it), the four
data directories and their permissions (including the state directory the watchers use), config and
token-store readability, per-character login state (token time left, auto-refresh, granted consent —
`orders` and `corp-orders` included), SDE document freshness and where each document resolves from — all
four of them, `blueprint_materials.json` last, whose absence is a warning naming `update-data` rather than
a blocker because it costs only `build-cost` — the registered callback URLs, SP-history age, and what the
watchers have accumulated:

- `watch.state` — watched characters, order owners (split into characters and corporations), known
  open orders, and how long ago any of them was last polled. A state that only holds training data is
  reported as healthy with a hint naming the poll that would start tracking orders; a corrupt file is
  a warning that says to move it aside, because the commands treat it as empty and keep working — what
  is lost is the baseline, so the next watch could re-announce whatever was in flight.
- `watch.events` — how many events are recorded, split between training and order kinds, per-kind
  counts, unreadable lines, and the age of the newest one.

Checks that can only be answered from mode bits — `path.config`, `path.cache`, `path.data`,
`path.state`, `config.file`, `permissions.tokens` — report `skip` on Windows with the reason (privacy
comes from the ACL inherited from the user profile, and every Windows `stat()` claims mode 666), not a
phantom warning with an unrunnable fix. Skips are counted in the summary line as `N skipped`, never as
problems, and they never change the exit code; on POSIX the same checks stay `ok`/`warn` exactly as
before. See [Platform support](#platform-support).

`--network` adds strictly opt-in, unauthenticated, bounded probes of the public SSO discovery document
and three public ESI documents — `/status`, `/meta/compatibility-dates`, and one real market order book
(Jita, `Tritanium`) — that classify *why* a request failed (DNS, timeout, TLS, refused, HTTP status)
instead of reporting "network error". The book probe is the only part that reads rate-limit data, since
`/markets/{region}/orders` is the one endpoint ESI budgets separately: it reports the book's age from
its own `Last-Modified` and how much of the 15-minute budget is left —
`public market data reachable: 160 order row(s), generated 3m 57s ago; rate-limit budget 11994 of
12000 left` — warns when the book is older than a day (that is upstream, not this tool), and reports a
420/429 with the `Retry-After` ESI asks for rather than hammering it. It runs only when `/status`
answered like ESI: one outage is reported once, so a captive portal or an ESI 503 leaves the book probe
as `skip`, not a second failure. `--timeout SECONDS` sets the per-request probe timeout.

What the report contains is deliberate. Credentials never appear: character diagnostics come from a
field whitelist, and every string in the report — text or JSON — passes a redactor seeded with the
credential values found on disk, so an access token, refresh token or client secret cannot escape
even inside an exception message (it prints as `[redacted]`). Paths are shown relative to your home
directory — `~/.config/eve-skills/tokens.json`, not `/home/you/.config/eve-skills/tokens.json`, and on
Windows `~\AppData\Local\eve-skills\config\tokens.json` rather than a profile path carrying your username —
because the expanded form would leak that username into whatever you paste the report into; a
location you configured explicitly outside your home is printed exactly as it is, since naming it is
what the check is for. A path inside a hint — advice meant to be pasted into a shell — is quoted in the
form that running platform's own shell expands, so the line runs as printed: POSIX gets
`chmod 755 "$HOME/.local/state/eve-skills"` (a tilde inside single quotes is literal, and no directory
of that name exists), Windows gets the profile variable in its place —
`"%USERPROFILE%\AppData\Local\eve-skills\state"` for that same directory, and
`move "<path>" "<path>.bak"` to set a damaged file aside — since cmd knows neither `~` nor `$HOME`.
What the report does keep is what the diagnostics are about: character names and ids, versions, file
modes where they mean something, counts, URLs and statuses.

Exit code is 1 only when something blocks the tool (unreadable/corrupt token store, an expired
login that cannot refresh, a service unreachable with no cached fallback); everything worth
knowing but survivable is a warning, and warnings exit 0.

---

## Multi-character behavior

- Every stored character is used unless `--char` selects one. `--char` accepts an exact character id,
  an exact name, or a unique case-insensitive substring; ambiguous or unknown values are errors that
  list what is stored.
- `skills` and `summary` isolate fetch failures per character and continue with the rest. Optional
  exports isolate missing consent per character; corporation job/asset authorization failures become
  warnings. Other ESI failures can still abort an export command.
- Commands that need a single subject (`plan`, `extract`) demand `--char` when several are stored.
- Add characters by running `login` again and choosing a different character in the browser; there is no
  bulk add.

---

## Where data lives, and how it is protected

### The four roots

One module — `eve_skills/paths.py` — decides where anything goes, and nothing else re-derives it. A
set and non-empty `$XDG_CONFIG_HOME` / `$XDG_CACHE_HOME` / `$XDG_DATA_HOME` / `$XDG_STATE_HOME` wins
on **every** platform, Windows included (an empty value counts as unset, and a pin is taken exactly as
given). With none set:

| Kind | POSIX default | Windows default | Why there |
|---|---|---|---|
| config | `~/.config/eve-skills` | `%LOCALAPPDATA%\eve-skills\config` | Local **on purpose**: this is where `tokens.json` keeps live refresh tokens and an optional client secret, and `%APPDATA%` is what domain profile sync and OneDrive Known Folder Move replicate — credentials must not leave the machine that way |
| cache | `~/.cache/eve-skills` | `%LOCALAPPDATA%\eve-skills\cache` | Regenerable — a roaming cache buys nothing but profile size |
| data | `~/.local/share/eve-skills` | `%LOCALAPPDATA%\eve-skills\data` | The SDE copy can be re-downloaded by `update-data`, so it must not roam |
| state | `~/.local/state/eve-skills` | `%LOCALAPPDATA%\eve-skills\state` | A watch state is machine-local by definition — the watchers, their baselines, their recorded alerts |

A missing `%LOCALAPPDATA%` falls back to the documented `AppData\Local` under the user profile;
only if even the profile cannot be located does a resolver return the POSIX-shaped path, because
naming the wrong tree beats naming nothing.

### The files

| File | Root | Contents | Sensitivity |
|---|---|---|---|
| `tokens.json` | config | Access + refresh tokens per character, granted scopes, client id/secret | **Secret — live credentials on both platforms.** Written `0600` where mode bits exist and private by inherited profile ACL on Windows; atomic replace, read-modify-write under `tokens.lock` so a running `--watch` and a manual command cannot corrupt each other's write |
| `config.json` | config | Client id/secret, optional `user_agent` | **Secret** when it holds a secret — written private, updated under `config.lock` |
| `sp-history.jsonl` | config | `{ts, char_id, total_sp}` rows, last 60 days | Non-secret, local-only SP history; pruned rewrites hold `sp-history.lock` |
| `watch-state.json` | state | Last queue observations per character, **plus the order owners being watched** under `owners` — one entry per `char:<id>` / `corp:<id>` with its open, pending and already-settled order ids (30-day retention). Both halves are what make watch alerts fire exactly once | Non-secret; deleting it only re-announces whatever was in flight |
| `events.jsonl` | state | Watch alert history shown by `events` — training and order kinds alike, 365-day retention | Non-secret; append-only JSONL, always written in binary mode so no platform can rewrite its newlines |
| `endpoints.json` | cache | SSO discovery document, cached 24 h | Non-secret |
| `names.json` | cache | Resolved id→name cache (skills, stations, systems, item types), merged under `names.lock` | Non-secret |
| `types.json` | cache | The type catalogue `inventory` builds: per type id its name, group and category — groups and categories cached as their own sections, so a new type in an already-known group costs one request. `version`-tagged, merged under `types.lock` | Non-secret (public universe data); deleting it only re-buys the fan-out for ids this machine has not met since |
| `quotes.json` | cache | The reduction of every order book `inventory --value-at` read: `min_sell` / `max_buy` per (region, station/system filter, type) plus that response's own `Last-Modified` and `Expires`. Never the order rows. `version`-tagged, merged under `quotes.lock`; entries past their `Expires` are dropped on the next write | Non-secret (public order-book figures); deleting it only costs a refetch |
| `{clone_grades,bloodline_races,skill_catalog,blueprint_materials}.json` | data | SDE snapshot from `update-data`; overrides packaged data. `blueprint_materials.json` is the one `build-cost` reads — every blueprint's activity and material list, keyed by the product it makes | Non-secret |
| Lock sentinels: `tokens.lock`, `config.lock`, `sp-history.lock` (config), `names.lock`, `types.lock`, `quotes.lock` (cache), `watch-state.lock` (state), `update.lock` (data) | beside the file they guard | Zero-length advisory locks, never read or written — `fcntl.flock` on POSIX, a byte-range lock on Windows | inert |

Every durable write goes through one helper: a **unique temporary** file in the destination directory
(`O_EXCL`, opened binary wherever that flag exists so a JSONL file cannot quietly gain CRLF; `0600` up
front for secret payloads) followed by an atomic rename — a crash leaves either the old file or the
new one, never a truncated mix, and two writers can never stomp on each other's temporary. Windows
renames atomically too, but refuses while another process still has the destination open, so the
rename is retried through that window (8 attempts across roughly 1.05 s) rather than losing the write.
Files updated by read-modify-write (token store, config, SP history, name cache, watch state) hold
their advisory lock across the whole read+write, and the OS releases it if the holder dies. Readers
resolve paths with `create=False`, so inspecting state never lays out a directory. An older
single-character `tokens.json` layout is migrated automatically on first load.

Security posture worth knowing:

- Access tokens are sent as `Authorization: Bearer` only to `esi.evetech.net`. Authorization codes,
  refresh tokens and confidential-client credentials are sent only to `login.eveonline.com`; both
  services are contacted over HTTPS with 30-second timeouts.
- The JWT returned by SSO is decoded **without signature verification** — this is a local desktop tool
  reading back its own freshly minted token. Nothing security-critical is decided from those claims.
- ESI responses are cached in-process only, keyed by token+path, with TTLs taken solely from the
  server's `Expires`/`Cache-Control` headers plus ETag revalidation. Nothing sensitive is written to
  disk from ESI apart from public id→name mappings.
- The tool requests read-only scopes; there is no write endpoint in the codebase.
- Backup/erase: the whole footprint is those four directories — config, state, cache, data — wherever
  your platform puts them (on Windows that is one roaming location plus three machine-local ones, so a
  wipe has to visit both roots). `eve-skills logout` removes tokens locally (all of them, or one with
  `--char`).

Network politeness built in: pinned `X-Compatibility-Date` (`2026-08-18`), a configurable User-Agent,
retries with backoff on 420/429/502/503 honouring `Retry-After`, and automatic pause when ESI's
`X-ESI-Error-Limit-*` headers show the error window filling up.

---

## Testing and verification status

The permanent automated suite runs in 520 tests, all passing, and is deterministic: no network, no
real credentials — every test works in a throwaway `$XDG_*` tree against fakes or fixtures, and those
pins are what let the Windows branches run on a Linux host, since they win on every platform.

```bash
cd eve-skills                                          # your checkout
uv run --with setuptools python -m unittest discover -s tests -t . -q   # 520 tests, no install
uv run python -m unittest discover -s tests -t . -q                     # 513: packaging tier skips
.venv/bin/python -m unittest discover -s tests -t .                     # the same suite, without uv
```

- `tests/test_eve_skills.py` — pure-logic units: clone-state tiers, queue labels (including
  CCP's dateless entries), SP-history baselines and the flat/dip rate guard, CSV contract;
- `tests/test_esi_transport.py` — ESI transport against a scripted in-process `urlopen`:
  pagination and its cap, retry policy and `Retry-After` handling, error-limit backoff, the
  market-order rate-limit group (its own budget, 5-token 4XX penalty, honouring `Retry-After`),
  response-header freshness (`Meta`, `get_meta`/`get_many`/`fold_meta`), server-clock tracking,
  ETag revalidation;
- `tests/test_persistence.py` — durable-write and token-lifecycle invariants: atomicity, `0600`
  permissions, lock exclusion — including real concurrent subprocesses racing the same files;
- `tests/test_paths.py` — the layout contract on both branches: `%LOCALAPPDATA%` placing each kind
  (config included: it holds live refresh tokens, so it must not follow a roaming profile onto a
  file server), a missing profile variable falling back to the documented `AppData\Local` folder,
  POSIX defaults unchanged, `$XDG_*` pins winning on either platform and an empty one counting
  as unset, `create=False` never touching disk on either branch, and every artefact landing in the kind
  its resolver names — which is also the check that no second resolver survived anywhere;
- `tests/test_storage_platform.py` — the Windows half of persistence against an injected backend: a
  host without `fcntl` selects `msvcrt`, a host offering both prefers `flock`, a host with neither fails
  loudly instead of running unlocked; byte-range contention retried until granted with 20 ms backing off
  to 250 ms, re-entrancy without taking a second OS lock, every call aimed at one byte at offset zero,
  and an unusable descriptor not retried forever; temporaries opened in binary wherever the flag exists,
  so what is written is byte-for-byte what comes back; a rename blocked by a sharing violation that
  clears being retried, one that never clearing re-raising with no temporary left behind, and an
  unrelated failure never retried at all;
- `tests/test_cli_integration.py` + `tests/fake_esi.py` — the real command handlers end-to-end
  against a deterministic fake ESI: multi-character token isolation, current live response
  shapes, JSON/CSV output contracts, graceful per-character degradation on fetch failure or
  missing consent. The `inventory` half pins the rewritten surface rather than its plumbing: all
  three views with their subtotal rows and labelled TOTAL, `--by category` coming out as the same
  money transposed, every row named although container ids overflow int32, nested items rendering the
  whole path, unpriced rows last and `-` rather than free, a hub priced at its own station and a region
  widened past it, the refused structure degrading to one labelled cell plus the fix command, the JSON
  document carrying ids beside names, CSV keeping stdout footnote-free, and an owner ESI refuses
  reported as that owner's problem instead of failing the run;
- `tests/test_universe.py` — the catalogue and location resolver behind `inventory`, against a fake ESI
  that answers exactly as the live one does: the three waves in their real order with each wave keyed by
  what the previous one said, groups/categories cached so a new type costs one request, a warm run silent
  on universe routes while the personal half is still re-read, two runs merging under the lock so neither
  loses an id or a name, ids outside ESI's int32 range never posted to `/universe/names` (a location id
  there fails every *other* name in the batch), chains that cycle or run past their depth limit cut at a
  stable label instead of hanging, a refused structure degrading to one labelled cell without poisoning
  the names beside it, ESI's `"None"` placeholder dropped so an unnamed item shows its type, and custom
  names asked for in chunks because ESI caps the ids per request;
- `tests/test_market.py` — type/scope resolution by name or id, quote maths (spread, margin, listed
  volume, empty side), the published reference price and its zero-versus-absent keys, the cluster scan
  and its partial-failure accounting, and the `market` command's text/JSON/CSV output including one
  freshness line per scope plus which of the four empty-book footnotes the run's coverage earns —
  station, region, whole-cluster scan or measured vault type — including that a hub book alone neither
  borrows PLEX's explanation nor quotes a figure, and that `/markets/prices` is requested exactly when a
  footnote will quote it, in every output format. It also covers the quote cache behind
  `inventory --value-at`: keys that carry the scope so a hub figure can never answer a region question,
  records served only while ESI's own `Expires` vouches for them, expired entries dropped on the next
  write, two runs publishing to one file without losing each other's types, a cached figure printed at
  the age ESI stamped rather than the moment it was read, and the run's pre-flight notice — what it
  counts, when it quotes a duration, and the all-cached form that promises no book is read;
- `tests/test_industry.py` — the cost model on its own, with no transport in sight: ME rounding to two
  decimals then up (and a one-per-run material that research cannot make disappear), job time scaling with
  runs and TE while a reaction ignores TE entirely, EIV built from base quantities so ME provably does not
  shrink the fee, the fee itself as EIV × index + tax + surcharge with `--facility-tax` replacing only its
  own term, recipe indexing where the lowest blueprint id wins and the rest are listed as alternatives,
  one-level expansion that never expands a recipe into itself, whole-run charging for built components,
  an unpriceable material excluded from every total rather than counted as free, the two research levels
  billing their own jobs and nothing else (so a cap-researched component level cannot flatter the top
  job, and an unresearched top level cannot cheapen a component), a reaction component taking no ME at
  any component level, validation of `--me`, `--component-me`, `--te`, `--runs` against each blueprint's
  own install limit, and the dated-rules warning firing only after its 180 days;
- `tests/test_build_cost.py` — the command end to end against a synthetic blueprint world on the same fake
  ESI: every material priced and the cheaper option charged, the printed fee recomputing from the printed
  EIV and rates, `--system` moving the index without moving the shopping, JSON keeping the losing build
  option visible (including per-unit cost of a component job beside its table figure), CSV as one row per
  material with every note on stderr, forcing flipping a row and reporting its surplus, an unpriceable
  material named in the notes with `cost per unit` withheld rather than rounded down, one book read per
  distinct type with a warm rerun reading none, two products sharing one fan-out, and five refusals —
  contradictory flags, a component level past the cap, a type no local blueprint makes, `--region`
  without `--system`, a system with no published index — that spend no order-book request at all;
- `tests/test_orders.py` — order normalisation from malformed and partial ESI rows (escrow optional,
  derived closed state), character + corporation fetching with its role diagnosis, and the `orders`
  command's table, totals and footnotes;
- `tests/test_planner_catalog.py` — prerequisite closure, ordering, coverage and rank pricing
  against synthetic catalogs, plus one check of the bundled SDE snapshot itself;
- `tests/test_watch_events.py` — the watch transition model for both halves (training and orders:
  silent first-sight backfill, once-ever settlement, the two-day wait before an unexplained closure,
  history-fetch failure freezing conclusions), durable exactly-once event history, the `events` command
  including `--kind`, `--char` and `--owner`, and the two console branches: a Windows console that
  refuses virtual-terminal processing gets the plain rule line while one that accepts it still clears,
  and `--notify` with no `notify-send` to run explains itself exactly once per process while the bells
  keep firing;
- `tests/test_doctor.py` — what doctor reports, its never-writes promise (the state directory included),
  the watch coverage checks, how each network failure class is classified, that nothing secret leaks
  into text or JSON, that no path is printed with the home directory expanded, and that a hint's
  command pasted into a shell runs as printed — plus the same report with the platform forced to
  Windows: the six mode-dependent checks skip and name the inherited ACL instead of warning, skips never
  block while a corrupt token store still fails with a `move` command cmd can actually run, no hint
  contains `chmod` or `$HOME`, and the report says which OS produced it;
- `tests/test_packaging.py` — metadata, license text, wheel/sdist contents, and a clean install
  of the built artifacts into a throwaway venv.

What remains **unautomated**: the real interactive SSO login/logout round trip (browser consent
cannot be scripted here), live ESI schema drift beyond the response shapes pinned in the fake, and
real SDE downloads beyond the test fixtures. Specifically for orders: **no live end-to-end order
announcement has been observed on this machine** — neither stored character holds the `orders`
consent, so a real fill has never passed through the watch here. That path (fetch → observe → record →
announce, personal and corporation) is proven by `tests/test_watch_events.py` against the fake ESI,
not by observation, and the README says so rather than implying otherwise.

Windows is now **verified on Windows**, not only reasoned about.
`.github/workflows/ci.yml` runs this suite on `windows-latest` and `ubuntu-latest` against Python
3.11 and 3.14, installs the package, and exercises both entry points and `doctor` there; all four
legs pass. Getting there took three rounds, and what the runner reported was worth having: two of
the failures were tests that had written the POSIX layout into an assertion about something else,
and three were real product bugs no forced-platform test could have caught — text writes taking the
ANSI code page instead of UTF-8, stdout doing the same when it is not a console, and `logout`
unable to delete a file another process held open. The forced-platform tests (an injected fake
`msvcrt`, an injected platform judgement) still earn their place: they pin *which* syscall
sequence, path, verdict and string each branch produces, which a green runner does not tell you.
What no runner here covers: how a roaming profile moves these files between machines, what an
antivirus holding the token store open does to a rename, and ConHost versus Windows Terminal for
the virtual-terminal fallback.

Proven here with uv 0.12.6 on Linux: `uv sync`, `uv run eve-skills --version`,
`uv run --with setuptools python -m unittest discover -s tests -t . -q` (520 tests) and the same
line without `--with setuptools` (513, packaging tier skipped — the reason the flag is documented),
`uv venv`, `uv pip install -e .`, `uv build`, and `uv tool install .` / `uv tool list` /
`uv tool uninstall eve-skills`. The suite also passes under Python 3.11, the floor
`requires-python` promises. Not run from this tree, only read out of `uv --help`: the
`git+https://…` tool install and `uv tool update-shell`. Every PowerShell line here was typed for a
shell nobody on this machine has.

Live read-only smoke evidence from this session (manual, not part of the suite): a 27-variant command
matrix including prerequisite-expanding `plan`, the compact watch dashboard, `events`, `doctor` and
`doctor --network`, plus `update-data` against the real SDE (build 3494416, a 592-skill catalog). The
market paths were re-run live for this wave: `market Tritanium --hub jita --hub amarr` (two scopes, two
freshness lines, both ~3 minutes old against ESI's five-minute book), `market Tritanium --global`
(70 regions scanned, cluster row stamped by the oldest book), `market … --region "The Forge" --history 7`
(daily volume plus its footnote) and `doctor --network`, whose new check reported
`public market data reachable: 160 order row(s), generated 3m 57s ago; rate-limit budget 11994 of
12000 left`. That first smoke run found two real defects — the `update-data` subcommand had been dropped
from the parser, and `doctor` aged SP history against a `None` clock — both were fixed, and each now has
a regression test. Treat the smoke runs as evidence for those paths; the repeatable guarantees live in
the suite.

The empty-book footnotes were then verified live against those same paths, with every request the tool
made logged: `market PLEX --hub jita` and `market PLEX --global` (0 orders either way, 70 regions
scanned) printed the vault footnote with ESI's own figures — average 4,574,918.36 ISK, industry adjusted
0.00 ISK, stamped 12m 00s old beside a book 2m 08s old — and each read `/markets/prices` exactly once.
The two narrower cases printed no figure: `market "Mystic XL" --hub rens` (2 orders in Heimatar, none at
Rens) suggested `--region "Heimatar"`, and `market 43687 --region Domain` pointed at `--global`; neither
sent a request for the price document. Nor does machine-readable output buy it any more: `market
Tritanium --hub jita --json` reported `"reference": null` with only `/universe/ids` and one regional book
fetched, and the same run's `--csv` row left the four `reference_*` cells empty. Footnotes stay per type
rather than per run even when one type has earned a figure: `market PLEX "Mystic XL" --hub rens` gave
PLEX its vault explanation and number, and Mystic XL — two orders in Heimatar, none at Rens — the wider
scope instead.

The rewritten `inventory` paths were measured live on one real holding of 518 distinct types, with the
requests and durations recorded rather than remembered: the type catalogue cold (518 + 168 + 18 requests
≈ 125 s) against the same holding warm (9 requests ≈ 3 s), and `--value-at` cold at 100.6 s / 518
requests, an immediate repeat at 14.1 s / 63 requests with 455 types served from the quote cache, then an
`--items` run straight after at 4.0 s / 18 requests with 500 cached — the figures quoted in
[inventory](#inventory--what-you-own-where-it-is-what-it-is-worth). Only counts and durations left that
machine, which is why they are here: what a run *listed* is somebody's holdings, and none of it belongs in
a public repository.

The `build-cost` paths were measured live the same way, with the request log open: one product cold spent
30 requests — 26 order books, one `/industry/systems`, one `/markets/prices` and two name lookups — and the
rerun straight after read no books at all, because ESI's own `Expires` still vouched for every figure. The
shape of a run was measured over the whole snapshot rather than assumed: pricing all 4943 products takes a
median of 8 distinct types, p90 19, and 71 at the worst (Vanquisher), and 2415 of them have at least one
direct material another blueprint makes — which is what turned the one-level substitution boundary
into an arithmetic decision instead of a hunch. The worked example in
[build-cost](#build-cost--what-manufacturing-an-item-costs-right-now) is that measurement: a Hound run
at ME 0 with its components at the default ME 10 totals 19,096,864.18 ISK against a cheapest standing
ask of 15,980,000.00, so the verdict printed beside it is *buy*; `--buy-all` (19,656,945.77) and
`--build-all` (19,298,269.07) were each re-run to confirm that neither forcing mode beats letting the
tool choose row by row, and `--component-me 0` on the same item lifted it to 19,509,454.53 — which is
what one assumption about component research is worth on a build this size. What a live run cannot
settle — order-book depth behind an ask, invention, who owns the blueprint — is carried in
[Limitations](#limitations-you-should-know) instead of being quietly priced.

---

## Limitations you should know

| Area | Behavior |
|---|---|
| Clone state is inferred | ESI has no "is omega" endpoint. `ALPHA` comes from a live clamp (`active < trained`) — but Expert Systems can also lower active levels on an omega character. `OMEGA` needs an unclamped beyond-cap skill, a catalog-known omega-only skill, or the *currently training* item targeting beyond-cap levels; future queue entries alone yield only `LIKELY_OMEGA`; clamp + omega evidence together is `CONFLICT` (stale ESI data or a just-changed clone); everything within limits is `UNKNOWN`. Skill state that local data cannot confirm never claims omega — it says refresh the SDE. |
| Jove / Triglavian | Have no alpha grade of their own; caps fall back to the union of all four faction grades (highest cap wins). |
| Queue gaps are real data | CCP omits schedule dates when an item cannot train; those rows show `BLOCKED` / "no schedule - cannot train" and must not be read as active training. |
| ESI lags finished training | A completed queue item can stay visible until the character logs in. The tool overlays the completed level and marks it pending (`*`) rather than pretending nothing happened. |
| Data freshness matters | Alpha caps warn after 90 days; extractor rules are dated constants that warn after ~180 days. Both warnings name the remedy or the verification date. |
| Pagination is capped | Paginated GETs follow at most 100 pages, so an enormous corp asset list would be truncated rather than loop forever. |
| Name resolution degrades | A structure this token may not see stays a labelled `structure <id>`, an id whose parent chain ESI never completes stays `location <id>`; name failures never fail the command. |
| `/universe/names` fails as a whole batch | ESI validates `ids` as int32 and answers **400 for the entire request** when one id overflows (verified live 2026-09-08) — and container, ship and structure ids are all far above that bound. So one citadel in a batch would cost every station name beside it: `inventory` sends type ids to `/universe/types` and location ids to their own resolvers, and never posts a location id to `/universe/names`. The fake ESI reproduces the whole-batch 400, which is what makes "every row is named" a real pin instead of luck. |
| Structure names need consent *and* access | `/universe/structures` answers only with `esi-universe.read_structures.v1`, and only for structures ESI lets this character see; anything else keeps its `structure <id>` cell, and the run prints `login --scopes structures` once. A token that provably lacks the scope is not probed at all — refusals spend error-window budget that throttles the rest of the run. |
| The reference price is not a quote | The default basis is CCP's published figure for an item: nothing can be bought or sold at it, and it does not move with the market. `--value-at` answers the tradable question — the richest standing buy at one scope — and even that is what *dumping* pays; what *listing* would raise is printed beside it from the same rows, never merged into the money column. |
| `--value-at` costs one order book per distinct type | ~0.19 s a type through eight workers measured, so a 518-type holding is 518 requests and a minute and a half cold. The run says so on stderr before it starts; the quote cache then re-serves each figure only while ESI's own `Expires` vouches for it, which is what makes a second look nearly free. |
| Consent is opt-in | A missing optional scope prints `no <feature> consent - run: eve-skills login --scopes <feature>` for that character and exits 0. Granting consent always requires your click in a browser; the tool will not do it for you. |
| Corp access is role-bound | Corporation jobs/assets need director / Account-Manager rights; ESI refusal becomes a per-character warning. |
| SP history is local and short | 60 days, best-effort, one row per successful fetch, written only when the write succeeds (a failed history write never breaks `skills`). A fresh install honestly reports "no baseline yet" for `--week`. |
| [Planning](#plan--training-plan-one-character) spans attributes | SP costs are exact (rank-based) and prerequisites expand automatically, but one calibrated SP/hour rate prices every row: skills driven by other primary/secondary pairs really train at other rates (the output names the pairs involved), remaps or implant changes during training shift real time, and implants are never modeled into future levels. `plan` needs the SDE skill catalog from `update-data`. |
| Order fill state is derived | ESI's order history only states `cancelled` or `expired`. An order that sold out matches neither, so a filled state is inferred from `volume_remain` reaching zero — and an order ESI explains with no history row at all is reported as `order_closed, reason unknown`, never guessed at. |
| No closure timestamp | ESI publishes no closed-at time. Fills witnessed between two polls are timed at the poll that saw them; closures read out of history are bounded by the order's own expiry and marked `[time estimated]`. In the `--closed` table, `expires` for a cancelled order is when it would have run out, not when it was pulled. |
| Order history is ~90 days | `orders --closed` and the watch backfill can only reach ESI's history window; older orders are simply gone from ESI. The first watch poll ingests that whole window silently — those rows are marked `[history]` in `events`, not announced as news. |
| Traded volume is daily, regional, one day behind | `market --history` comes from a different document than the order book: it has no station granularity (a hub row shows its region's trades), and the newest day is yesterday. The cluster row carries none. |
| `--global` means k-space + Pochven | The cluster scan walks region ids 10000000-11000000 — 70 regions today. Nullsec, wormhole and unlisted markets are not in it, and a region that fails to answer is reported as missing rather than counted as having no orders. |
| Market freshness has a floor | ESI regenerates each regional book at most every five minutes, so `Last-Modified` ages below that are not this tool being slow and cannot be improved by polling harder. Nothing here caches a market book to disk: if ESI will not answer, `market` says so instead of showing a stale price. |
| No third-party price source | Fuzzwork, EVERef and similar aggregators are deliberately not consulted — they are other people's copies of the same public books, with their own staleness, availability and terms, and no way for this tool to be told one is wrong. |
| Vault-traded items have no book | PLEX (id 44992) trades on the account-wide vault market, which belongs to no region's order book: `GET /markets/{region}/orders?type_id=44992` answered `[]` for all 70 market regions when measured 2026-09-07, while `/markets/prices` carried the type the same minute (`average_price` 4,574,918.36, `adjusted_price` 0.0). ESI publishes no global order-book endpoint, so there is nothing wider to ask; `market` says so for that id and shows the published reference rather than leaving a row of dashes to be read as a broken tool. No other type id has been measured across the cluster, so no other empty book is given that explanation — it gets the wider scope that is still unasked instead. |
| `build-cost` prices the cheapest ask, not the depth behind it | The material column charges a unit at the lowest standing ask in scope; ESI publishes no cumulative quantity per price level, so nothing here knows how many units that ask actually holds. Measured in Jita the error is ~0-1% even at 1000 runs — Tritanium there is effectively bottomless — but a thin market is another matter: the Hound sell side in Heimatar opens with one unit 8.7% below the blended cost of ten, so a quantity that size has to be swept further up a book this tool cannot see and would cost more than printed. |
| Invention is not modelled | A T2 item's real cost is understated: no datacores, no decryptors, no probability-weighted attempts — only the manufacturing run after you already have a blueprint. The SDE carries the inputs for that stage and they were deliberately left out rather than half-modelled, so treat a T2 figure as a floor, not as what invention costs. |
| Blueprint ownership and structure bonuses stay outside the number | BPO/BPC acquisition is excluded — the recipe is priced as if you already own it — industry skills are ignored because they change job time and not materials, and structure or rig bonuses reach the maths only through `--material-multiplier`, which is one flat factor for the whole job rather than the real stack of modifiers. |
| Two research levels, not one per job | `--me` bills the blueprint you run; `--component-me` (default ME 10, since a component's BPO is usually long since researched to the cap) bills every component job. That is two levels where a real build has one per blueprint: nothing below a component job is ever built, so a component's own inputs are bought at their asks whatever you research, and a level named for a recipe that cannot be researched — a reaction — is dropped rather than applied somewhere else. |
| The install fee's rates are CCP policy, not physics | The 4% SCC surcharge and the 0.25% NPC facility tax were measured against CCP's published rules on 2026-09-09 and warn once they are more than 180 days old. `--facility-tax` exists because a player-owned structure bills differently and only you know its rate; the system's cost index is ESI's own, from `/industry/systems`. |
| ESI/SSO availability | Discovery (cached 24 h), token exchange, ESI routes and the SDE download are all remote services; failures surface as `error: network error ...` or `error: HTTP <code> ...`. |
| Desktop notifications are Linux-only in practice | `--notify` shells out to `notify-send`, which no stock Windows installation ships; there the watch says so once per run and relies on the terminal bell. Alerts are printed and recorded on both platforms, so nothing is lost but the ping. |

---

## Repository layout (maintainers)

```text
eve_skills/
  cli.py         parser + command handlers, gather(), text/JSON/CSV rendering, watch loop
  doctor.py      read-only installation diagnostics (never writes, redacts secrets)
  sso.py         OAuth2 PKCE login (loopback + manual), refresh, scope registry, token/config storage
  esi.py         stdlib ESI client: caching, retries, error-limit backoff, server-time, name cache
  universe.py    asset identities: disk-cached type/group/category catalogue + nested location resolver
  market.py      public order books: type/scope resolution, quote maths, cluster scan, freshness + history
  industry.py    blueprint recipes, ME/TE and job-time maths, EIV + install fee, one-level build-or-buy
  orders.py      character/corporation order fetching, normalisation, access (consent vs role) diagnosis
  classify.py    alpha-cap lookup, per-skill classification, clone-state inference
  alphadata.py   packaged/user SDE data loading, transformations, update-data downloader
  planner.py     rank-based SP costs, prerequisite expansion, rate calibration, extractor math
  snapshots.py   local SP history (60-day JSONL)
  watchstate.py  watch observations, exactly-once transitions, durable event history
  storage.py     unique-temp atomic writes + advisory file locks (flock on POSIX, byte-range on Windows)
  paths.py       the only resolver of the config / cache / data / state directories, on either platform
  exports.py     standings / jobs / inventory (grouped, valued) / travel / implants + consent hints
  render.py      timestamps, SP/duration formatting, plain-text tables
  data/          packaged SDE snapshot (clone_grades, bloodline_races, skill_catalog, blueprint_materials)
tests/           unittest suite: pure units, ESI transport, persistence concurrency + the injected
                 Windows lock backend, path layout on both branches, fake-ESI command integration,
                 planner catalog, market (+ quote cache), industry cost model and `build-cost` end to end,
                 universe catalogue/locations, orders, watch/events, doctor (POSIX + forced Windows),
                 packaging
pyproject.toml / LICENSE / RELEASE.md   packaging metadata, the GPL-3.0 text, the release procedure
```

Conventions: standard library only — adding a runtime dependency needs a deliberate decision.
Console entry point is `eve-skills = eve_skills.cli:main`; `python -m eve_skills` calls the same
function, so both paths must stay equivalent. New commands belong in `cli.py` (core) or `exports.py`
(consent-gated views), take `--char` with the shared semantics, and degrade per character rather than
aborting a multi-character run. Optional consent always goes through `sso.OPTIONAL_SCOPES` +
`exports.targets()` so a missing grant stays a hint. Keep parser help text, this README and the scope
table in sync when adding a feature name. A new OS difference belongs in `paths.py` (where a directory
lives) or `storage.py` (how a file is locked and replaced), chosen by capability — module availability,
not a version guess — so command handlers never learn the platform at all.
