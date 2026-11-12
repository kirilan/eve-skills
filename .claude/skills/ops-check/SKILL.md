---
name: ops-check
description: Quick status check of an EVE Online industry operation run with eve-skills - finished jobs waiting for delivery, free slots, jobs ending soon, undercut or outbid market orders, inputs about to run out, open to-dos, and optionally profit so far - ending in a short prioritized list of what needs attention. Use when the user asks "check the jobs and orders", "is there anything that needs attention", "I have an hour, what can I do", or "how is the accounting looking".
---

# Ops check: what needs attention now

A triage pass, not a plan. Find what is waiting on the user, rank it, then hand the details to the
specialised skill: **industry-jobs** to plan jobs, **market-trip** for orders and sell runs,
**supply-run** for shopping, **bulk-sell-pricing** for paste strings.

Commands are written `eve-skills ...`. In a source checkout run `.venv/bin/python -m eve_skills ...` (and
`.venv/bin/python` for the scripts) from the repository root.

## 0. This installation first

```bash
eve-skills operation show                   # sites, sellers, planning settings
eve-skills operation playbook ops-check     # this installation's own rules
```

**Playbook rules win over this skill's defaults.** With no playbook, use the defaults below and say which
choices you guessed.

## 1. Gather

```bash
eve-skills ledger sync && eve-skills ledger progress && eve-skills ledger note show   # if a ledger is kept
python .claude/skills/industry-jobs/job_board.py [--site NAME]   # slots, ready jobs, ends, short inputs
eve-skills orders --char SELLER --check                          # orders vs books, undercut first
eve-skills prices signal                                         # dip / high / falling for what we hold
```

`prices signal` is offline (ledger + the hourly snapshots). Its `prices synced` line should be under ~2 h
old; if not, the installation's hourly `prices sync` has stopped (`eve-skills doctor`).

For money questions add `ledger pnl [--since DATE] [--by week]`, `ledger products`, `ledger invention`,
`ledger inventory`. Every money figure goes out with the report's *assumptions* block (unknown ME or
decryptor, pre-ledger stock, fees matched by rate): never present those figures as exact.

## 2. Rank

1. Finished jobs not delivered (a ready job holds its slot), and free slots that could start now, within
   or near a finish window (`planning.finish_windows`).
2. Inputs or decryptors that will block the next batch, and the **invention backlog**: idle T2 copy runs
   plus expected runs from running invention, in days of build use. Under a day with free labs means
   invention is short (industry-jobs, "Size invention to manufacturing").
3. Undercut sell orders on stacks worth more than a day of volume; outbid buy orders. Only the issuing
   character can reprice, from the order's region, so these may wait for a trip.
4. Price states on stock we hold: `HIGH` stock not listed is a reason to sell soon; a `DIP` on a line
   about to be listed means list only the signal's share; `FALLING` on a line we keep building is worth a
   line review (`build-cost --sell-at`). A stale `prices synced` is a fix-the-timer item.
5. Open to-dos that are due.
6. Nothing to do: say so, and give the time of the next event (next job end, next window).

Data notes: jobs are cached about 5 minutes, assets and blueprints about an hour, orders about 20 minutes;
right after installs a character can read 0 jobs. Training queues: accounts train one character at a
time, so an idle queue on a second character of the same account is expected; flag only an account with
nobody training. A daylight-saving change moves job ends in local time: around it, mention ends that slip
out of a window.

## 3. Reply format

Lead with the verdict ("Two things need attention: ..."), then one short section per item with the
concrete action and when. Include data timestamps (jobs, assets, orders caches). Offer the relevant skill
as the next step. If the installation keeps a ledger, record what the user decides as a note.

## Where new rules go

Something true for any operation belongs in this skill, through a pull request; it must pass the
private-terms check. This installation's characters, accounts, sites and dated findings go into its
playbook: `eve-skills operation playbook ops-check --path`.
