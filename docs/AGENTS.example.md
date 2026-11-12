# Agent instructions (example)

A starting point for an installation's own `AGENTS.md` (or `CLAUDE.md`) in the repository root. That file
is git-ignored: it describes your operation, so it stays on your machine. Everything below is invented;
replace it with your own.

## Use the CLI before ad-hoc ESI analysis

For EVE industry or market questions, check the existing `eve-skills` commands before writing throwaway
ESI scripts or guessing type ids. Read [docs/agent-reference.md](agent-reference.md) before scripting
against CLI JSON or the ledger.

- `operation show` lists this installation's sites, facilities, hubs and freight routes. Values marked
  NOT SET are defaults, not facts: say so.
- `industry status --json` for a diffable snapshot (`--corp` for a corporation site).
- `jobs --times --site NAME` for measured hours per run and window-fitting run counts; `jobs --since
  HH:MM --site NAME` for what was installed since then.
- `types NAME ...` for type ids, groups and packaged volume; `ledger sql "SELECT ..."` and `ledger schema`
  for read-only questions to the ledger.
- `production round list --site NAME` and `production round check --site NAME` resumes the saved round; use this before reconstructing a
  plan from notes. `production round save FILE --site NAME` saves the agreed jobs; close a finished or
  superseded round explicitly. Input format and full workflow: [production.md](production.md).
- The pool is handed over as a whole stack. Check aggregate remaining material needs, observed holder,
  exact structure and item hangar versus Deliveries. Do not issue per-builder material batches merely
  because their hangars are empty. Record `production round handoff` only after the user confirms it;
  report confirmation separately from ESI evidence and its next refresh.
- `production supply --site NAME --days N --lead-days N` for depletion and purchase deadlines, local
  bridge purchases and depth-aware haul costs. Unfilled buy orders are never stock.
- `production replenish --site NAME --hub KEY` connects sales, held stock, manufacturing and invention;
  list/haul existing goods before adding jobs. Untested lines need an explicit trial target.
- `production scan ITEM ... --site NAME` for comparable candidate economics; inspect lab capacity,
  eligibility, fees and assumed market share before expanding production.
- `production facility impact` explains historical bonus adjustments. Recording a historical facility
  profile requires dated evidence; preview before using `--apply`.
- `build-cost --brief`, `market --depth N`, `sell-plan`, `orders --check`, `restock`, `prices signal`.

Run commands with `.venv/bin/python -m eve_skills` in a source checkout. Tests:
`.venv/bin/python -m unittest discover -s tests -t .`. Treat cache timestamps, staleness warnings and
unknown values as part of the answer.

## Skills and the playbook

| Request | Skill |
|---|---|
| "plan the next jobs", "what can I install now", "did I miss anything" | `industry-jobs` |
| "plan a supply run", "what do I need from the hub" | `supply-run` |
| "are our orders ok", "what should I bring to the hub" | `market-trip` |
| sell prices / MultiSell paste | `bulk-sell-pricing` |
| "anything need attention?" | `ops-check` |

Each skill reads `eve-skills operation playbook <skill>` first; the playbook's rules win over the skill's
defaults. When a session finds a new rule: one about this operation goes into the playbook
(`eve-skills operation playbook NAME --path` prints the file), one true for any operation goes into the
skill through a pull request.

## The operation (example)

- Sites: `home` (personal: invent at Example Lab, build at Example Factory), `corp` (research).
- Characters: Alice Builder and Bob Inventor build; Carol Hauler holds the pool and sells.
- Jobs should end 07-09 or 19-21 local time.

## Ledger and progress

If you keep the ledger: start a session with `ledger sync`, then `ledger progress` and `ledger note show`.
Record progress as notes (`ledger note add "title" --body-file -`), close to-dos with `ledger note done ID`.
Every money report ends with an *assumptions* block: report it with the figures.
