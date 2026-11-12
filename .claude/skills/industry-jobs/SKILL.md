---
name: industry-jobs
description: Plan the next industry jobs for an EVE Online operation run with eve-skills - what to deliver, which manufacturing, invention, copying and research jobs to install on which character and facility, and how many runs so each job ends when someone can deliver it. Use when the user asks "plan the next jobs", "what can I install now", "are we ready for the next batch", "I delivered the jobs, what's next", "what invention should I start", "did I miss anything" after installing, or whether an idle copy or a line is worth a slot.
---

# Industry jobs: plan the next batch

Goal: an exact, installable job list (who, blueprint with its runs, facility, where the inputs must be,
end time) from live data, in one pass.

Commands are written `eve-skills ...`. In a source checkout run `.venv/bin/python -m eve_skills ...`, and
the board with `.venv/bin/python .claude/skills/industry-jobs/job_board.py`, from the repository root.

## 0. This installation first

```bash
eve-skills operation show                       # sites, the facility of each activity, planning settings
eve-skills operation playbook industry-jobs     # this installation's own rules
```

**Playbook rules win over this skill's defaults.** A playbook may name companion files (`eve-skills
operation playbook NAME`); read the ones it says apply to the request. With no playbook, work on the
defaults below and say which choices you had to guess. Values `operation show` marks NOT SET are
defaults, not facts: say so when an answer depends on one.

## 1. Gather

```bash
eve-skills ledger sync && eve-skills ledger progress && eve-skills ledger note show   # if the playbook keeps a ledger
python .claude/skills/industry-jobs/job_board.py [--site NAME] [--start HH:MM] [--since HH:MM]
```

The board reads one site of the operation file and prints:
- slots per character (used/max, **ready** = finished but not delivered, free, next end);
- jobs ready to deliver and running jobs by end time, flagged when outside a finish window;
- idle blueprints by what they can do (`industry status` `activities` / `invents`): copies to build,
  copies to invent from, originals. Each row names its facility and says `haul to X first` when the
  activity runs elsewhere. Lines with no job in the history window are counted, not listed
  (`--all-blueprints`);
- measured hours per run where the site does the activity, and with finish windows the run counts that
  end inside one (`~` = converted from another facility by the time multipliers; `-` = no measurement);
- personal sites: a pool check (`restock --site`) listing every input still short, and the `restock
  --invent` command for the invention copies (run it once you chose the attempts);
- inputs per facility and holder, including items parked in a structure's Deliveries hangar.

Other views: `jobs --times --site NAME --finish-window HH-HH --start HH:MM --json` (timing alone),
`jobs --since HH:MM --site NAME` (what was installed since), `industry status --json`, `blueprints
--copies --items --json` (runs per copy), `can-build --site NAME --builders A,B --type ITEM`.

Is a line worth a slot? `eve-skills build-cost ITEM [ITEM ...] --site NAME --sell-at HUB --seller NAME
--brief` gives margin per unit and per job-hour on the blueprint's base time; for a close call pass
`--hours-per-run` with the builder's measured figure from the board. Judge a line at the lower of its
current list price and its 30-day average: a price spike makes any line look good.

## Save and resume the round

Read [the production workflow](../../../docs/production.md) for the round JSON format. Check
`production round list --site NAME` before making another plan. Resume with `production round check
--site NAME`: report installed, changed, remaining, blocked and awaiting-refresh lines separately.
Save the agreed jobs with `production round save FILE --site NAME`; close a completed or superseded
round explicitly. Notes explain decisions; the saved round is the job checklist.

The checker reserves shared inputs across every remaining job and recalculates finish estimates from
now. For whole-stack operations, give the installer sequence and the current holder/site/hangar;
pass all remaining blueprints and materials after each installer finishes installing. Do not allocate
per-builder material batches. Flag scattered copies and Deliveries stock before calling a round ready.
Record `production round handoff` only for a user-confirmed transfer; confirmation and cached ESI
holdings are separate evidence. Do not infer a missed install while jobs are awaiting refresh.

Use `production replenish --site NAME --hub KEY` before expanding a line: list or haul existing output
first, then consider WIP, idle copies and invention. Use `production scan ITEM ... --site NAME` for a
new product comparison; it includes invention costs and lab capacity. Own hub sales and regional-share
scenarios are different evidence. Unknown eligibility, fees or timing need checking before installation.

## 2. Decide

- **Deliver first.** A ready job holds its slot until it is delivered. Say so up front.
- **Finish times** (choice; default: none). With `planning.finish_windows` (or `--finish-window`) the
  board sizes runs so jobs end in those hours. A longer job that ends in a window beats a shorter one that
  ends while nobody can deliver. If nothing fits, give the off-window end and let the user choose.
- **Leftover runs** (choice; default: offer both). Window sizing can leave a few runs on a copy. Some
  operations would rather run the whole copy than keep a short remnant that costs another install; offer
  the full-copy option with its end time next to the window count unless the playbook decides.
- **One blueprint item runs one job at a time**: parallel jobs need separate copies. A copy job is one job
  with runs = copies wanted, not N one-run jobs. Size T1 copy runs to whole invention jobs so no remnant
  is left.
- **Copies that can build or invent** (choice: `planning.t1_copies`, default both). A T1 copy can be built
  or used for invention; the board lists it where the setting says.
- **Invention.** Attempts <= runs left on the T1 copy; one decryptor per attempt when one is used.
  Chance = base x (1 + (science 1 + science 2) / 30 + encryption / 40) x decryptor. Decryptors change the
  chance, the invented copy's runs, ME and TE:

  | Decryptor | Chance | Runs | ME | TE |
  |---|---|---|---|---|
  | none | x1.0 | +0 | +0 | +0 |
  | Accelerant | x1.2 | +1 | +2 | +10 |
  | Attainment | x1.8 | +4 | -1 | +4 |
  | Augmentation | x0.6 | +9 | -2 | +2 |
  | Optimized Attainment | x1.9 | +2 | +1 | -2 |
  | Optimized Augmentation | x0.9 | +7 | +2 | +0 |
  | Parity | x1.5 | +3 | +1 | -2 |
  | Process | x1.1 | +0 | +3 | +6 |
  | Symmetry | x1.0 | +2 | +1 | +8 |

  An invented copy starts at ME 2 / TE 4 and the T2 blueprint's base runs before the decryptor's change.
  Compare decryptors by cost per T2 run made (datacores + decryptor + job fee, divided by expected runs)
  plus the material saved by ME; prefer ones that make more runs per lab hour when labs are the limit.
- **Size invention to manufacturing, not to free labs** (default: keep 1-2 days of T2 copy runs ahead of
  the build slots). Backlog = (runs on idle T2 copies + attempts running x success rate x runs per
  success) / runs the build slots use per day. Under a day: fill the labs. Over two days: cut attempts,
  lowest margin first.
- **Stock limit** (choice; default: about two weeks of your expected sales per line, `prices signal`
  `hold_cap`; its `cap basis` says whether that rests on measured sales or, for a new line, on a share of
  the region's volume - `prices signal --rates` shows each hub's figure). Above it, park the line's copies instead of building more. `planning.never_build` lists
  products never to propose.
- **Builders.** Check skills before assigning a line to a character that has not built it:
  `can-build --site NAME --builders A,B --type ITEM`. Read clone state and skills live (`summary`,
  `skills`), never from notes.
- **Materials.** A `buy` line in the pool check means the job is blocked: cut that line's jobs to what
  the stock covers and say what to buy (hand over to the supply-run skill).
- **Stock passing** (choice): a corp site shares hangars (set each job's Input Material Location to the
  division that holds the inputs); a personal site needs the blueprint and every input in the
  **installer's own item hangar**, either handed to each builder or held by one pool holder
  (`sites.X.pool_holder`) who passes the whole stack builder to builder. Items received with a
  structure's "Deliver to" land in the Deliveries hangar; move them to the item hangar before installing.
- **A site with a facility per activity** invents at one facility and builds at another: copies made at
  the lab must be hauled to the build facility before they can be installed.

## 3. Reading the data

- **Cache ages.** Blueprints and assets refresh about hourly, jobs about every 5 minutes, orders about every
  20. The board prints each document's age. Right after installs a character can read 0 jobs: wait for the
  next jobs refresh before calling it idle.
- **Snapshots and deliveries.** `industry status` drops or reduces copies used by jobs delivered after the
  blueprint snapshot (`blueprint_adjustments`); copies made by invention or copying since then are not
  listed until the next refresh. Count them from the delivered jobs.
- **A copy inside a job** shows the runs it had before the job. A T1 copy comes back from invention with
  runs minus attempts; plan the next round from that.
- On invention job rows `runs` reads `attempts x runs per success`; use `blueprints --copies` for real runs.
- **Which decryptor did a running job use?** The job's success probability (ledger `jobs` table) is the
  base chance times the decryptor's chance factor.
- Times converted from another facility (`~`) are estimates: re-measure after the first jobs there.
- Out of scope: reactions and planetary industry (`eve-skills pi`, `eve-skills colonies`).

## 4. Reply format

1. One line of state: time (local and UTC), data age, what is ready to deliver.
2. Per install round (now / at HH:MM after X lands): `Who | Blueprint (runs) | Jobs | Facility / input
   location | Ends`, manufacturing and invention separately. **Always write the runs**: copies of one
   line come with different runs. Write names that paste into the in-game search: the full name or a
   contiguous part of it, never abbreviations.
3. When stock is passed between characters: the install order (who gets the stack in which turn, and
   which copies with which runs they install).
4. The pool check: covered, or which input is short and how many jobs that costs; expensive inputs left
   for the next round.
5. T1 copy coverage per invented line, and a copy job for any line with a round or less left.
6. When the next batch is due, the saved round ID, and relevant ledger notes for decisions.

**"Did I miss anything?"** Use `production round check --site NAME` and give the remainder, changed
installs and refresh blockers. With no saved round, use `jobs --since <plan time> --site NAME` to compare
against the prior note; save the next agreed round so later checks are structured.

## Where new rules go

Something true for any operation (a game mechanic, how to read a tool's output) belongs in this skill,
through a pull request; it must pass the private-terms check. Something about this installation - its
characters, lines, facilities, prices, dated decisions - goes into its playbook: `eve-skills operation
playbook industry-jobs --path` prints the file to edit.
