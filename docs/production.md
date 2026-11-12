# Production workflow

`production` connects a saved plan with jobs, shared stock, supply needs and sales. All game access is
read-only. Saving a round, confirming a handoff, closing a round, and explicitly applying a facility
observation write to the local ledger; reports never place orders, transfer items or install jobs.

Start with `operation show`, the installation's `operation playbook industry-jobs`, and `ledger sync`.
A site and its activity facilities need location IDs. Personal sites read their characters' inventories;
corporation sites read the shared corporation through consenting readers. Missing consent, timestamps
and failed reads remain unknown. Reports include source timestamps and next refresh times.

## Save and continue a round

Create a JSON plan from the job board and the decisions you intend to execute. `product` is the finished
item's exact name or type ID, including for invention and copying. `runs` means manufacturing runs,
invention attempts, or copies requested. A copying line also needs `licensed_runs` (runs per copy).
Timestamps are ISO with a time zone. Omit `start` to use the time the round is saved.

```json
{
  "title": "Morning round",
  "lines": [
    {"product": "Damage Control II", "installer": "Alice Builder",
     "runs": 10, "jobs": 2, "me": 2, "te": 4},
    {"product": "Damage Control II", "installer": "Bob Inventor",
     "activity": "invention", "runs": 4, "decryptor": "none"}
  ]
}
```

```bash
eve-skills production round save round.json --site home --json
eve-skills production round list --site home
eve-skills production round show --site home --json
eve-skills production round check --site home --json
# Only after the user confirms a transfer:
eve-skills production round handoff --site home --facility factory --holder "Alice Builder"
# Close when finished or deliberately superseded; history is retained.
eve-skills production round close --site home
```

There is one active round per site. An explicit round ID can be used with `show`, `check`, `close` and
`handoff`. A plan is immutable: close it and save a replacement when intentions change substantially.
`jobs` expands into individually matched lines; use `blueprint_id` to pin a specific copy to one job.
A line can include `hours_per_run` as a labelled estimate, `probability` as an explicit invention
success assumption, and a `prerequisites` list of text steps. ME defaults to zero, so name the actual
copy ME; a different-ME copy is not silently substituted. Research and copying use the original of the
product you name. Invention uses its T1 source copy.

Check results:

- `installed`: a matching job, delivered jobs included.
- `changed`: same installer/activity/blueprint/facility, different runs or licensed runs.
- `remaining`: still to install; exact matches are allocated before changed matches.
- `blocked`: confirmed shortage, unavailable blueprint, skills/slots, or an outstanding prerequisite.
- `awaiting_refresh`: the documents cannot support a conclusion yet. A missing ESI row immediately
  after an install is not proof the user missed it.

The pool check reserves materials for **all remaining lines together**. One bulk stack can supply each
installer in turn; empty individual hangars do not generate itemized handover lists. It reports the
observed holder(s), site and usable versus Deliveries/container stock. A confirmed handoff is labelled
as user evidence until ESI catches up. It does not fabricate a new asset snapshot. If assets predate
installs, availability stays unknown instead of spending consumed inputs twice. Exhausted copies from
jobs delivered after the blueprint snapshot are removed. One copy cannot supply simultaneous jobs.

Remaining jobs get finish estimates from the current time or their later planned start, using measured
times at the facility where available. Estimates assume the listed prerequisites and free slots;
`jobs --times --site home --finish-window ...` provides alternate run counts. The checker does not
silently change planned runs. Unknown science skill requirements need manual verification or fresh SDE.

## Forecast the supply run

```bash
eve-skills production supply --site home --days 14 --history-days 7 --lead-days 2
# Inspect requirements without market calls:
eve-skills production supply --site home --no-prices --json
```

The active round supplies scheduled demand. `--round ID` selects another round from the same site.
Historical use is rebuilt from site jobs with the ledger's ME/decryptor/facility assumptions. Expected
running and saved invention output is a separate scenario, capped by measured available manufacturing capacity;
unknown decryptors or times stay unknown. Outputs beyond the horizon are excluded, and scheduled
builds reserve capacity and expected copies first. This conservatively uses currently free slots;
slots still occupied by running or ready jobs are not promised for future output. The horizon demand is the larger of explicit/expected demand
and historical-rate demand, avoiding double-counting the same production twice.

Each material reports current cover, the first blocked line where known, depletion time, order-by date,
and a fallback date for an unfilled bid. Buy orders are shown separately and **never counted as stock**.
The local bridge quantity covers demand before a haul can arrive. `--hub` selects the local market;
`--input-hub` selects the source market (default Jita). Quotes walk available sell-order depth and show
partial fills rather than pricing a large purchase entirely at the first ask. Freight is based on the
site's inbound route; the combined contract must also cover the route minimum.

Refresh the ledger and check the source timestamps before buying. Historical consumption can be a poor
forecast after a line-mix change; the saved round lets you provide explicit demand. Read warnings when
some expected production could not be modelled.

## Replenish listings before adding production

```bash
eve-skills production replenish --site home --hub jita --days 14 --json
# Explicit target for a new line with no proven sales:
eve-skills production replenish --site home --hub jita --target "Damage Control II=20"
```

Demand defaults to your own sales at that hub divided by the last 30 calendar days. `--history-days`
changes the window. Open orders are read live; sales remain ledger facts. The report combines listed
stock, unlisted goods at the hub and elsewhere, manufacturing, idle copy runs and expected invention.
It recommends `list`, `haul`, `build`, `invent` or `pause`, with `refresh`/`inspect` for incomplete data.
An untested line asks for a trial target. Regional volume is not used as if it were station demand.
Each hub is a separate scenario; allocate shared stock once when preparing trips to multiple hubs.
Material and slot checks still precede an actual build. Respect the playbook's keep list and one-order
policy when turning suggested replenishment into a listing.

## Compare products consistently

```bash
eve-skills production scan "Damage Control II" "Expanded Cargohold II" --site home --hub jita --seller "Carol Hauler"
eve-skills production scan - --site home --broker-pct 1 < candidates.txt
```

The scan reuses `industry.plan_build`, the live input books, system indices and the site's facilities.
It evaluates decryptors and includes failed invention attempts in expected cost. Candidate output shows
material and invention cost, eligibility, factory/lab hours, capital per day and the constraining
resource. Net price is capped at the destination's regional 30-day average.

These are **standalone line scenarios**, not an optimized portfolio. `--factory-slots` and `--lab-slots`
(default one each) are a shared scenario budget; do not sum profits across rows. `--regional-share`
(default 0.01) is an explicit, unproven demand assumption. Structure broker fees remain unknown unless
provided with `--broker-pct`. Invention uses conservative base chance with the decryptor; measured build
times may reflect different blueprint TE. `--copy-cost` budgets copy acquisition per invention attempt, or per manufacturing copy for a
non-invention line (default zero, explicitly excluded); copying time and BPO investment are not modelled. Fresh `update-data`
snapshots carry invention skill requirements; older ones show unknown eligibility. The scan caps the regional scenario by your own recent hub sales when available, and shows historical
ledger unit cost alongside the projection. Historical costs combine destinations and weighted purchases;
current quotes are not realized profit. Trial jobs and repeat sales are needed before scaling a new line.

## Historical facility costing

```bash
eve-skills production facility list --json
eve-skills production facility impact --json
# Preview the change first; this command does not write without --apply.
eve-skills production facility record --facility factory --effective 2030-01-02T08:00:00Z --reason "Verified install screen"
# Append the same observation when its date and settings have been checked.
eve-skills production facility record --facility factory --effective 2030-01-02T08:00:00Z --reason "Verified install screen" --apply
```

Full ledger syncs record changed facility settings effective at the sync time. This is an observation,
not proof of when the owner changed the structure. Older jobs keep the prior dated profile, or the
existing no-bonus assumption if none covers their start. Explicit backdating needs evidence and previews
its effect on job costs, stock and profit before application. Observations cannot be updated or deleted;
append a dated correction instead. Historical report snapshots remain records of what was reported then.

Weighted-average stock, opening prices and invention-cost pooling are unchanged. `facility impact`
compares the dated replay with the legacy no-bonus replay. Job-cost deltas are not net-profit deltas.
Fee reports distinguish matched/unmatched journal charges but remain **unverified**: matching alone
cannot prove that every structure broker fee was recorded.
