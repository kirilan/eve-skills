# Agent reference

One page for an agent (or a person scripting against the tool) to read instead of probing: how to run
things, what each `--json` document looks like, what the ledger tables hold, and how to use the package
from a script. Prefer a CLI command over a throwaway script; when a question needs one that does not
exist, say so in the session so it can be added.

## 1. Running things

- CLI: `.venv/bin/python -m eve_skills COMMAND ...` from the repository root (`python` may not be on
  `PATH`; the virtual environment's interpreter always is). `COMMAND --help` lists every flag.
- Tests: `.venv/bin/python -m unittest discover -s tests -t .`. `pytest` is not a dependency; the suite
  is plain `unittest` and needs no network (a fake ESI in `tests/fake_esi.py`).
- There may be no `sqlite3` binary: query the ledger with `ledger sql "SELECT ..."` (read-only) and see
  its tables with `ledger schema [TABLE]`, or use Python's `sqlite3` module.
- From a git worktree, run the worktree's code with `PYTHONPATH=$PWD`: the virtual environment's
  `eve-skills` entry point imports the checkout it was installed from. Check once with
  `python -c "import eve_skills; print(eve_skills.__file__)"`.
- Test an operation-file change without touching the real one: `EVE_SKILLS_OPERATION=/path/draft.json`.
- Every command reads cached ESI documents; cache times, staleness warnings and unknown values are part
  of the answer. Never replace a missing consent or an unrefreshed document with an assumed zero.

## 2. `--json` shapes

Values are replaced by their types (`str`, `int`, `float`, `bool`, `null`; `a|b` when either occurs).
`[...]` is a list of the row shown; `{<key>: ...}` is a map whose keys are data (a name or an id).
Timestamps are ISO 8601 strings in UTC (`...Z`). `cache` / `documents` entries are
`{last_modified: str, expires: str}` from ESI's own headers.

### `inventory --items --json`

```
generated: str, owner_kind: str, grouped_by: str, hints: [str], warnings: [str]
value_basis: {key, label, short: str, scope: str|null, requests, priced_types: int, unpriced_types: [str],
              failed_books: int, freshness: str, alternative: str|null, cached_figures, substituted_types: int}
characters: [{character_id: int, name: str, asset_rows: int, documents: dict|null,
  totals: {types, units: int, value: float, priced_units, priced_types, unpriced_types: int},
  groups: [{name: str, types, units: int, value: float, unpriced_units: int,
            entries: [{name: str, types, units: int, value: float|null, unpriced_units: int}]}],
  items: [{item_id, type_id: int, name, type_name: str, custom_name: str|null, group_name, category_name: str,
           quantity: int, singleton: bool, flag: str, division_number: int|null, division: str|null,
           location_id: int, location_kind: str, location_path: [{id: int, name: str, kind: str}],
           unit_price: float|null, value: float|null}]}]
```

`--corp` puts corporations under the same keys. `inventory --csv` columns, in this order: `character`,
`item_id`, `type_id`, `item_name`, `quantity`, `singleton`, `flag`, `location_id`, `location_name`,
`group_name`, `category_name`, `custom_name`, `location_path`, `location_kind`, `price_basis`,
`price_scope`, `unit_price`, `value`, `unpriced_types`, `division`.

### `blueprints --items --json`

```
owner_kind: str, idle_requested: bool, grouped_by: str, hints: [str], warnings: [str]
owners: [{name: str, character_id: int|null, corporation_id: int|null, notes: [str], documents: dict|null,
  blueprints: [{item_id, type_id: int, type: str, location_id: int, location, flag, division, kind: str,
                runs, me, te, count: int}]}]
```

`kind` is `BPO` or `BPC`; `runs` is -1 on an original.

### `jobs --json`

```
grouped: bool, hints: [str], warnings: [str]
documents: [{owner: str, corporation_id: int|null, cache: dict|null,
  jobs: [{job_id: int, status, activity: str, activity_id, product_id: int, product, runs, time: str,
          facility_id: int, installed_in: str, installer_id: int, installer: str, start_date, end_date: str,
          duration_h: float, hours_per_run: float}]}]
```

`status` adds `ready` (ended, not delivered) and `paused` to ESI's own values. `runs` is text: `10`,
`1 x60` (one copy of 60 runs), `3/10` (successes of attempts). `--group` rows: `installer`, `status`,
`activity`, `product`, `runs`, `count`, `time`.

### `jobs --times --json`

```
days: int, time_zone: str, site: str|null, facilities: {<activity>: str}|null, unscaled: [...]|null,
finish_window: str|null, start: str|null, max_hours: float|null, hints: [str], warnings: [str]
documents: [{owner: str, corporation_id: int|null, cache: dict|null,
  times: [{activity, product, installer: str, facility_id: int, facility: str, hours_per_run, min_hours,
           max_hours: float, jobs: int, scaled_from: str|null, fits: [{runs: int, end: str}]}]}]
```

`scaled_from` names the facility a time was converted from (`--site`); `fits` appears with
`--finish-window`. `unscaled` rows: `owner`, `activity`, `product`, `installer`, `facility_id`,
`facility`, `jobs`.

### `jobs --since WHEN --json`

```
since: str, time_zone: str, site: str|null, hints: [str], warnings: [str]
documents: [{owner: str, corporation_id: int|null, cache: {last_modified: str, expires: str},
  jobs: [{installer: str, installer_id: int, activity, product: str, product_id, runs, count: int,
          facility_id: int, facility: str, blueprint_ids: [int], start, end, status: str}]}]
```

### `industry status --json`

```
owner_kind: str, hints: [{code, feature, scope: str, refused_character_ids: [int], owner_id: int,
                          owner_name, section: str, fallback?: str}], warnings: [...]
owners: [{owner_id: int, owner_name: str, read_by_character_id: int|null,
  availability: {jobs, blueprints, assets, orders: bool},
  documents: {jobs, blueprints, assets, orders, divisions?: dict|null},
  characters: [{character_id: int, character_name: str, complete: bool, next_end: str|null,
                documents: {personal_jobs, skills: dict|null},
                slots: {manufacturing|science|reaction: {used, ready: int|null, max: int|null, free: int|null,
                                                         next_end: str|null}}}],
  jobs: {ready: [...], running: [{installer_id: int, installer_name: str, activity_id: int, activity: str,
         product_type_id: int, product: str, runs: str, count: int, end_start, end_end: str|null}]},
  blueprints: [{blueprint_type_id: int, blueprint: str, product_type_id: int|null, product: str|null,
                location_id: int, location: str, division_number: int|null, division, kind: str,
                runs_per_job: int|null, me, te, count: int, can_build_jobs: int|null,
                limiting_type_id: int|null, limiting_material: str|null, have, per_job: int|null,
                activities: [str], invents: [str]}],
  materials: [{type_id: int, type_name: str, quantity, location_id: int, location: str,
               division_number: int|null, division: str}],
  orders: [{order_id, type_id: int, type_name: str, is_buy: bool, price: float, volume_total, volume_remain,
            filled: int, filled_pct: float|null, location_id: int, location_name: str, region_id: int,
            region_name: str, issued_by: int|null, issued_by_name: str|null}],
  deliveries: [{type_id: int, type_name: str, quantity, location_id: int, location: str, other_station: bool}],
  blueprint_adjustments: {dropped, reduced: int},
  warnings: [{owner_id: int, subject: str, delivered_jobs?: int, dropped?: int, reduced?: int,
              snapshot?: str, message?: str}]}]
```

### `market TYPE --json`

```
generated: str, history_days: int|null
types: [{type_id: int, name: str, reference: dict|null,
  scopes: [{scope: str, region_id: int, region_name: str, location_id: int|null, location_name: str|null,
            min_sell, max_buy, spread, margin_pct: float|null, sell_volume, buy_volume, sell_orders,
            buy_orders: int, best_sell_location_id: int, best_sell_location_name: str, best_sell_region_id: int,
            best_sell_region_name: str, best_buy_location_id: int, best_buy_location_name: str,
            best_buy_region_id: int, best_buy_region_name: str, regions_scanned, regions_failed: int,
            last_modified, expires: str, age_seconds: float,
            history: {region_id: int, region_name: str, days, rows, total_volume: int, volume_per_day,
                      average_price: float, newest_date: str},
            depth: {sell|buy: [{order_id: int, price: float, volume, location_id, region_id: int}]}}]}]
```

`history` appears with `--history N`, `depth` with `--depth N`.

### `restock ... --json`

```
generated: str, build_station: {id: int, name: str}, hub: str, cargo_m3: float|null, haul_m3: float,
buy_isk: int, hints: [str], notes: [str]
lines: [{kind, label: str, units, fit: int, status: str, haul_m3: float}]
materials: [{type_id: int, name: str, m3_per_unit: float, need, on_site, pickup, move, buy: int,
             unit_price, buy_isk, haul_m3: float}]
price_check: {hub: str, cover_days: float, cap_days: int, items: [...], notes: [str]}
```

With `--site` on a personal site, also: `site: str`, `owner: str`,
`holdings: {<character>: {<type_id>: int}}`, `transfers: [...]`, `deliveries: {...}`,
`freight: {...}`; when the site invents and builds in different facilities:
`stations: {manufacturing|invention: {id: int, name: str}}` and
`holdings_by_station: {<station_id>: {<character>: {<type_id>: int}}}`.

### `build-cost TYPE --json`

```
generated: str, warnings: [str]
scope: {label: str, region_id, system_id: int, region_name: str, location_id: int|null}
job: {system_id: int, system: str, activity_indices_used: {<activity>: float}, facility_tax, scc_surcharge,
      material_multiplier, time_multiplier: float, facility?: str}
products: [{product_id: int, name: str, blueprint_id: int, activity: str, alternatives: [...], runs, me, te,
            component_me, units: int, material_cost, eiv, job_cost, total, cost_per_unit: float,
            time_seconds: int, market: {min_sell, max_buy: float|null}, unpriced, eiv_missing: [...],
  materials: [{type_id: int, name: str, required: int, source: str, forced: bool, unit_cost, cost: float,
               surplus: int, buy: {unit: float, basis: str}, build: dict|null}]}]
figures: {fetched, cached, failed: int, last_modified, expires: str, age_seconds: float}
```

### `orders --check --json`

```
generated: str, owner_kind: str, book: str, filters: {type_id: int|null, side: str|null}, matched, shown: int
owners: [{owner_key, owner_name: str, history_ok: bool, as_of, expires: str}]
orders: [{order_id: int, owner_key, owner_name: str, is_buy, is_corporation: bool, type_id: int, type_name: str,
          region_id: int, region_name: str, location_id: int, location_name: str, price: float, volume_total,
          volume_remain, filled: int, state, issued: str, duration: int, expires: str, escrow: float|null,
          min_volume: int|null, range: str, wallet_division: int|null, issued_by: int|null,
          issued_by_name: str|null,
          check: {best_other: float|null, units_ahead: int, days_ahead: float|null, ignored: [...], status: str,
                  suggest: float|null, volume_per_day: float, book_as_of: str}}]
totals: {sell_orders: int, sell_isk: float, buy_orders: int, buy_escrow_isk: float, buy_escrow_missing: int}
```

### `prices signal --json`

```
cutover, last_sync, prices_synced: str, fee_pct: float, warnings, notes, assumptions: [str], note_counts: {...}
items: [{type_id: int, type, state, action: str, price: float, price_source: str, normal, spread, z,
         week_vs_normal_pct, deviation_pct, stock: float, unit_cost: float|null, sold_per_day: float,
         sales_rates: [{hub: str, sold: float, listed_days: float|null, prior: float, ceiling: float|null,
                        rate: float, basis: "measured"|"blend"|"prior"|"flat"}], cap_basis: str|null,
         hold_cap: float|null, hold, sell: float, target: float|null, net_now: float, reason: str,
         past_dips: {dips, recovered: int, median_days: float|null, median_return_pct, mean_return_pct: float,
                     wins: int, rate: float}}]
```

`sold_per_day` is the expected rate the cap uses (sum of `sales_rates[].rate`), not raw sales / 30:
for our own products each hub's rate is units sold per listed day shrunk toward a regional-volume prior.

`state` is one of `dip`, `high`, `falling`, `normal`, `thin` (or null). The document and rows include `hub`.

### `ledger inventory --json`

```
cutover, last_sync, prices_as_of: str, warnings, notes: [str], note_counts: {...}
items: [{type_id: int, class: str, held, in_sell_orders: int, unit_cost: float, cost_basis: str,
         cost_value, unit_market: float, market_basis: str, market_value: float, type_name: str}]
work_in_progress: [{job_id, activity_id, blueprint_type_id, product_type_id, runs: int, end_date: str,
                    cost: float, installer_id, output_qty: int, unit_market, expected_value: float|null}]
totals: {material: {...}, finished: {cost, market: float, unpriced_types: int}, wip: {cost, expected_value: float}}
```

### `ledger invention --json`

```
cutover, last_sync: str, warnings, notes: [str], note_counts: {...}
invention: [{blueprint_type_id, jobs, attempts, attempts_done, successes: int, runs_made, cost_done,
             cost_running, materials, copy, fees: float, decryptor_id: int|null, decryptor_known: bool,
             decryptor: str, success_rate, cost_per_attempt, cost_per_run: float, product_type_id: int,
             cost_per_unit: float, product_name: str}]
```

### `operation --json`

```
file: str, configured: bool, default_site: str|null
sites: {<site>: {owner: str, facility: str, facilities: {<activity>: str}, characters, readers: [str],
                 hub, freight_in, freight_out, seller, pool_holder: str|null}}
facilities: {<facility>: {system: str, label: str|null, location_id: int|null, tax: float|null,
                          material_multiplier: {<activity>: float}, time_multiplier: {<activity>: float},
                          derived_from: str|null, unknown: [str]}}
hubs: {<hub>: {label: str, location_id, system_id, region_id: int, reader: str|null}}
freight: {<route>: {per_m3, collateral_pct, min: float}}
planning: {timezone: str|null, finish_windows: str|null, never_build: [str], t1_copies: str}
selling: {keep: [str]}
```

### `production ... --json`

See [production.md](production.md) for the accepted round input and workflows. The saved
`production_rounds.document` is normalized JSON (`version`, `site`, `title`, `created_at`, `start`,
`lines`); each line has a stable `line_id`, numeric character/product/blueprint/facility IDs,
`materials` keyed by type ID, runs, ME/TE and explicit assumptions. Saving expands `jobs` into lines.
An active site has one round; closing retains its document.

- `round check`: `round_id`, `site`, `generated`, `lines` (saved fields plus `state`, `observed_job`,
  `blockers`, `selected_blueprint`, `earliest_start`, `estimated_end`, `timing_basis`), `unplanned_jobs`,
  `pool`, `holders`, `installer_sequence`, `confirmed_handoffs`, `documents`, `warnings`, `assumptions`.
- `supply`: `generated`, `days`, `history_days`, `lead_days`, `items` with `facility_id`, `type_id`,
  `held`, `daily_use`, `planned_need`, `buy`, `bridge_buy`, `depletion`, `first_blocked`, `order_by`,
  `bid_fallback_by`, `open_bid_units`. Quotes add `local_purchase`, `hub_purchase`, `bridge_purchase`:
  `requested`, `filled`, `unfilled`, `cost`, `landed_cost`, `average`, `complete`, `as_of`, `expires`.
- `replenish`: `hub`, `items`: `type_id`, `target`, `sold`, `daily_sales`, `demand_basis`, `listed`,
  `held_at_hub`, `held_elsewhere`, `manufacturing`, `copy_units`, `invention_expected`, `action`,
  `list_or_haul`, `production_gap`. Run each hub as a scenario; never double-allocate shared stock.
- `scan`: `site`, `hub`, `input_hub`, `items` with candidate status, costs, margins, eligibility,
  factory/lab hours, demand assumption, bottleneck, capital and decryptor variants. Unknown net
  margins are null, never zero.
- `facility impact/record`: `changed_jobs`, `job_cost_delta`, `stock_cost_delta`, `pnl_before`,
  `pnl_after`, `fee_quality`, `applied`; `record` also includes the proposed `observation`.

`documents` carries owner, section, availability, `as_of` and `expires`. Stock/shortages are null
when freshness or completeness does not support them. `pool_handoffs` is user testimony, not an ESI
asset poll. `facility_history.profile` is an immutable JSON facility snapshot, applied by job start
and effective time. Neither table should be overwritten to make a report agree with a guess.

Production scans include `realized` ledger product figures (all destinations),
`projected_minus_realized_unit_cost`, and `demand_basis`. Ledger report headers include `fee_quality`
with matched/unmatched observed charges and an explicit unverified completeness status.

### Commands with `--csv`

`skills`, `events`, `standings`, `jobs`, `can-build`, `blueprints`, `inventory`, `travel`, `implants`,
`market`, `sell-plan`, `build-cost`, `colonies`, `pi chain`, `pi planet-type`, `system`, `orders`,
`ledger sql`, `types`. Hints and notes go to stderr in CSV mode, so stdout stays machine-readable.

## 3. Ledger tables

`ledger.sqlite3` in the data directory. Read it with `ledger sql` (read-only) and `ledger schema`.
Every table and column, as `ledger_db.py` creates them:

| Table | Columns |
|---|---|
| `meta` | `key`, `value` |
| `jobs` | `job_id`, `owner`, `activity_id`, `status`, `installer_id`, `facility_id`, `blueprint_id`, `blueprint_type_id`, `product_type_id`, `runs`, `licensed_runs`, `successful_runs`, `probability`, `cost`, `start_date`, `end_date`, `completed_date`, `raw`, `first_seen`, `last_seen` |
| `transactions` | `wallet`, `transaction_id`, `date`, `type_id`, `quantity`, `unit_price`, `is_buy`, `client_id`, `location_id`, `journal_ref_id`, `raw` |
| `journal` | `wallet`, `id`, `date`, `ref_type`, `amount`, `context_id`, `context_id_type`, `first_party_id`, `second_party_id`, `raw` |
| `orders` | `order_id`, `owner`, `type_id`, `is_buy`, `issued`, `issued_by`, `price`, `volume_total`, `volume_remain`, `state`, `location_id`, `raw`, `last_seen` |
| `blueprints` | `item_id`, `owner`, `type_id`, `quantity`, `me`, `te`, `runs`, `first_seen`, `last_seen` |
| `prices` | `type_id`, `day`, `average_price`, `adjusted_price` |
| `opening_prices` | `type_id`, `price`, `source`, `basis`, `fetched` |
| `sync_log` | `source`, `synced_at`, `rows_seen`, `rows_new`, `oldest`, `newest` |
| `notes` | `id`, `ts`, `kind`, `title`, `body`, `status`, `closed_at`, `source` |
| `snapshots` | `synced_at`, `data` |
| `market_history` | `region_id`, `type_id`, `day`, `average`, `highest`, `lowest`, `volume`, `order_count` |
| `book_snapshots` | `location_id`, `type_id`, `ts`, `polled_at`, `best_sell`, `best_buy`, `sell_units`, `sell_orders`, `buy_units`, `buy_orders`, `sell_units_1pct`, `sell_units_5pct`, `our_sell_units`, `our_best_sell` |
| `watchlist` | `type_id`, `mode`, `changed` |
| `stock_snapshots` | `ts`, `type_id`, `class`, `held`, `listed`, `unit_cost`, `unit_market` |
| `pi_polls` | `ts`, `as_of`, `sources` |
| `pi_holdings` | `ts`, `type_id`, `held`, `listed` |
| `production_rounds` | `id`, `site`, `title`, `created_at`, `closed_at`, `document` |
| `pool_handoffs` | `id`, `round_id`, `facility_id`, `holder_id`, `at`, `hangar`, `note` |
| `facility_history` | `id`, `facility_id`, `effective_at`, `recorded_at`, `reason`, `profile` |

Traps met in earlier sessions:

- There is **no `types` table** and no name column anywhere: use `type_name(type_id)` in `ledger sql`
  (and `character_name(id)` for installers and issuers).
- `journal` has **no `balance` and no `owner` column**: the owner is in `wallet` (`corp:<id>:<division>`
  or `char:<id>`), as in `transactions`.
- `watchlist` has **no `name`**: join nothing, call `type_name(type_id)`.
- Timestamps are ISO UTC text: `local_time(ts[, zone])` gives `YYYY-MM-DD HH:MM` in a zone.
- `raw` columns keep ESI's whole row as JSON (`json_extract(raw, '$.field')`).
- `ledger sync` writes (and upgrades the file to the running version's schema); `ledger sql` never writes.

## 4. Using the package from a script

Prefer a CLI command (most take `--json`); say in the session when one is missing. When a script is
still the right tool:

```python
from eve_skills import esi as esi_mod, exports, sso

client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))   # cached, rate-limited ESI client
cid = sso.resolve_character("Alice Builder")                         # stored character name or id -> id
tok = sso.get_access_token(cid)                                      # refreshes when needed; {"access_token", "scopes", ...}
doc = client.get(f"/characters/{cid}/industry/jobs", token=tok["access_token"])
rows, meta = client.get_all_meta("/markets/10000002/orders?type_id=34")   # every page, plus cache headers
names = esi_mod.resolve_names(client, {34, 60003760})                # ids -> names, disk-cached
```

- `exports.targets(args, [(feature, scope)])` returns `(client, [(token, public_doc)], hints)` for the
  stored characters that granted a scope (respecting `args.char`); commands use it to degrade a missing
  consent to a hint instead of failing.
- `market.resolve_type(client, "Tritanium")` -> `(34, "Tritanium")` (exact names only);
  `universe.type_info(client, ids)` gives group, category and volumes from a disk cache.
- `operation.load()` reads the operation file (`EVE_SKILLS_OPERATION` overrides its path).
- Never print or copy token documents; never call `login`, `logout` or `alerts setup` from a script.

## 5. Not covered by the CLI yet

- Structure service modules and fitting (what a structure has online) and dogma attributes of items:
  ESI reads or local SDE work, no command.
- Gate routes: `system A --route B` gives the jump count (`--flag secure|insecure|shortest`); there is
  no command that lists the systems on the way.
