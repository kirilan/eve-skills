# eve-skills

Command line tool for EVE Online characters: skills, training queue and alpha/omega clone state,
plus opt-in views for standings, industry jobs, asset inventory, location/jump clones, implants,
training plans and Skill Extractor math - and market data: live order books and prices for any item
in any region or trade hub, your own open and closed orders, and a watch that announces the moment
one of them fills, expires or is cancelled.

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
| `chars` | Stored characters, access-token time left, auto-refresh availability | offline (no network) |
| `events` | Recorded watch alerts: training finished / queue emptied / your orders filled, expired or cancelled | offline (no network) |
| `standings` | Agent / NPC corp / faction standings | `--scopes standings` |
| `jobs` | Personal or `--corp` industry jobs | `--scopes jobs` |
| `orders` | Your own open orders with price, remaining volume, escrow and time left; `--closed` for ESI's ~90-day order history; `--watch` announces fills/expiries; `--corp` for corporation orders | `--scopes orders` (and `corp-orders` for `--corp`) |
| `inventory` | Assets: per-location summary, `--items`, or full `--csv` | `--scopes assets` |
| `travel` | Current location, home, jump clones with their implants | `--scopes location` and/or `clones` |
| `implants` | Implants fitted in the active clone | `--scopes clones` |
| `plan` | Ordered, priced training path to target levels incl. auto-added prerequisites | no — needs the SDE skill catalog (`update-data`) |
| `extract` | Skill Extractor count, injector yield, re-training cost | no |
| `update-data` | Refresh alpha caps and the full skill catalog from the official SDE (~100 MB download) | no |
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
uv run python -m unittest discover -s tests -t . -q      # the suite, also without installing
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
# %APPDATA%\eve-skills\ on Windows (see [where data lives](#where-data-lives-and-how-it-is-protected)):
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
  `orders`, `corp-orders`, `all`. An unknown name is a hard error listing the choices.
- **Attributes need no scope of their own.** `attributes` (and exact `plan` costs) use
  `esi-skills.read_skills.v1`, which every login already requests; `login --attributes` is accepted
  for clarity and asks for nothing beyond the core skills consent.
- **Market prices need no consent at all** — `market` reads the public order books, so it works on a
  fresh install with nothing configured. Only *your own* orders are private: `orders` needs the
  `orders` consent, and `orders --corp` additionally needs `corp-orders` plus the in-game Accountant
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

### `standings`, `jobs`, `inventory`, `travel`, `implants`

```bash
eve-skills standings --csv > standings.csv
eve-skills jobs                       # personal jobs
eve-skills jobs --corp --completed    # corp jobs incl. finished/cancelled
eve-skills inventory                  # per-location/flag summary: types, units, singletons
eve-skills inventory --items          # every asset row
eve-skills inventory --csv            # always per-item, incl. item_id/type_id/location_id
eve-skills travel                     # current location, home, jump clones + their implants
eve-skills implants                   # implants in the active clone (one row per fitted instance)
```

All five accept `--char` and `--csv`. In CSV mode any consent hint is written to **stderr** so stdout
stays machine-readable. Corporation variants (`jobs --corp`, `inventory --corp`) additionally need
the matching director / Account-Manager role on that character; without it ESI refuses and the tool
prints a per-character warning rather than failing the whole command.

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

### `update-data` — alpha caps and skill catalog from the official SDE

```bash
eve-skills update-data              # latest build (~100 MB download)
eve-skills update-data --build 3494416   # example: pin a known specific build
```

Downloads the official JSONL SDE zip from `developers.eveonline.com`, extracts clone grades,
bloodline races and the full skill catalog (name, rank, attributes and prerequisites for every
catalogued skill), and atomically replaces three files in the data directory
(`$XDG_DATA_HOME/eve-skills`, `%LOCALAPPDATA%\eve-skills\data` on Windows). That
user copy takes precedence over the snapshot shipped in the package, so you can refresh caps and
the catalog without touching the checkout. `plan` is built on this catalog — without one it
refuses with `no local skill catalog - run: eve-skills update-data`. The whole download runs
under `update.lock`, so two concurrent runs cannot both pull ~100 MB and interleave builds, and
each file is replaced atomically. `skills` warns when the local snapshot is more than 90 days old
(the age line also names the SDE build in use).

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
`orders` and `corp-orders` included), SDE document freshness and where each document resolves from,
the registered callback URLs, SP-history age, and what the watchers have accumulated:

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
Windows `~\AppData\Roaming\eve-skills\tokens.json` rather than a profile path carrying your username —
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
| config | `~/.config/eve-skills` | `%APPDATA%\eve-skills` | Roaming **on purpose**: on a roaming profile the credentials and settings follow the user between machines |
| cache | `~/.cache/eve-skills` | `%LOCALAPPDATA%\eve-skills\cache` | Regenerable — a roaming cache buys nothing but profile size |
| data | `~/.local/share/eve-skills` | `%LOCALAPPDATA%\eve-skills\data` | The SDE copy can be re-downloaded by `update-data`, so it must not roam |
| state | `~/.local/state/eve-skills` | `%LOCALAPPDATA%\eve-skills\state` | A watch state is machine-local by definition — the watchers, their baselines, their recorded alerts |

A missing `%APPDATA%` / `%LOCALAPPDATA%` falls back to the documented `AppData\Roaming` /
`AppData\Local` under the user profile; only if even the profile cannot be located does a resolver
return the POSIX-shaped path, because naming the wrong tree beats naming nothing.

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
| `{clone_grades,bloodline_races,skill_catalog}.json` | data | SDE snapshot from `update-data`; overrides packaged data | Non-secret |
| Lock sentinels: `tokens.lock`, `config.lock`, `sp-history.lock` (config), `names.lock` (cache), `watch-state.lock` (state), `update.lock` (data) | beside the file they guard | Zero-length advisory locks, never read or written — `fcntl.flock` on POSIX, a byte-range lock on Windows | inert |

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

The permanent automated suite runs in 397 tests, all passing, and is deterministic: no network, no
real credentials — every test works in a throwaway `$XDG_*` tree against fakes or fixtures, and those
pins are what let the Windows branches run on a Linux host, since they win on every platform.

```bash
cd eve-skills                                          # your checkout
uv run python -m unittest discover -s tests -t . -q    # 397 tests, with no install step at all
.venv/bin/python -m unittest discover -s tests -t .    # the same suite, without uv
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
- `tests/test_paths.py` — the layout contract on both branches: `%APPDATA%` / `%LOCALAPPDATA%` placing
  each kind, a missing profile variable falling back to the documented `AppData\Roaming` / `Local`
  folder, POSIX defaults unchanged, `$XDG_*` pins winning on either platform and an empty one counting
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
  missing consent;
- `tests/test_market.py` — type/scope resolution by name or id, quote maths (spread, margin, listed
  volume, empty side), the published reference price and its zero-versus-absent keys, the cluster scan
  and its partial-failure accounting, and the `market` command's text/JSON/CSV output including one
  freshness line per scope plus which of the four empty-book footnotes the run's coverage earns —
  station, region, whole-cluster scan or measured vault type — including that a hub book alone neither
  borrows PLEX's explanation nor quotes a figure, and that `/markets/prices` is requested exactly when a
  footnote will quote it, in every output format;
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

And no **Windows machine was involved at all**. Nothing here has run on Windows: the `msvcrt` lock
backend, profile-folder path resolution, doctor's skips and cmd-shaped hints, the virtual-terminal
fallback and the notify explanation are exercised by forcing the platform — an injected fake `msvcrt`,
an injected platform judgement — from a Linux host. That is real coverage of what those branches *do*
(which syscall sequence, which path, which verdict, which string), and it is not evidence that Windows
itself behaves as documented: whether `%LOCALAPPDATA%` really resolves where Microsoft says, how a
roaming profile moves these files between machines, what ConHost accepts, and what an antivirus holding
the token store open actually does to a rename remain untested until someone runs them there. Proven
here with uv 0.12.6 on Linux: `uv venv`, `uv pip install -e .`, `uv run eve-skills --version`,
`uv run python -m unittest discover -s tests -t . -q`, and `uv tool install .` / `uv tool list` /
`uv tool uninstall eve-skills`. Not run from this tree, only read out of `uv --help`: the
`git+https://…` tool install and `uv tool update-shell`; the other `uv run …` lines are the proven
mechanism with different arguments. Every PowerShell line here was typed for a shell nobody on this
machine has.

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
| Name resolution degrades | Private structures and unresolvable ids stay as `id 1234567890`; ESI name failures never fail the command. |
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
  market.py      public order books: type/scope resolution, quote maths, cluster scan, freshness + history
  orders.py      character/corporation order fetching, normalisation, access (consent vs role) diagnosis
  classify.py    alpha-cap lookup, per-skill classification, clone-state inference
  alphadata.py   packaged/user SDE data loading, transformations, update-data downloader
  planner.py     rank-based SP costs, prerequisite expansion, rate calibration, extractor math
  snapshots.py   local SP history (60-day JSONL)
  watchstate.py  watch observations, exactly-once transitions, durable event history
  storage.py     unique-temp atomic writes + advisory file locks (flock on POSIX, byte-range on Windows)
  paths.py       the only resolver of the config / cache / data / state directories, on either platform
  exports.py     standings / jobs / inventory / travel / implants views + consent hints
  render.py      timestamps, SP/duration formatting, plain-text tables
  data/          packaged SDE snapshot (clone_grades, bloodline_races, skill_catalog)
tests/           unittest suite: pure units, ESI transport, persistence concurrency + the injected
                 Windows lock backend, path layout on both branches, fake-ESI command integration,
                 planner catalog, market, orders, watch/events, doctor (POSIX + forced Windows), packaging
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
