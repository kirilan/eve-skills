---
name: supply-run
description: Plan a restock trip for an EVE Online industry operation run with eve-skills - work out what the planned builds and inventions need, net it against the stock the characters already hold, fit it to the hauler's cargo or a courier contract, check prices against their normal, and produce a Multibuy paste list. Use when the user says "check inventory and plan a supply run", "calculate the restock trip", "do I need anything from the hub", "we ran out of materials", or asks what to buy for the next jobs.
---

# Supply run: what to buy and haul

Commands are written `eve-skills ...`. In a source checkout run `.venv/bin/python -m eve_skills ...` (and
`.venv/bin/python` for the scripts) from the repository root.

## 0. This installation first

```bash
eve-skills operation show                    # sites, facilities, hubs, freight routes, planning settings
eve-skills operation playbook supply-run     # this installation's own rules
```

**Playbook rules win over this skill's defaults.** With no playbook, use the defaults below and say which
choices you guessed (horizon, hauler, where to buy). NOT SET values in `operation show` are defaults,
not facts.

## 1. Decide the demand

Start with `production supply --site NAME --days N --history-days 7 --lead-days N` after `ledger sync`.
It combines the active saved round, recent consumption and expected invention, reports the first
blocked line/depletion date, and prices local bridge versus hub purchases through actual book depth.
See [production workflow](../../../docs/production.md) for assumptions and JSON fields. Choose the
horizon and lead time from the playbook; the command defaults (14 and 2 days) are only scenarios.
Unfilled bids are never stock: name the fallback deadline and the immediate purchase needed if they
remain unfilled. A partial quote is not a full purchase budget. Unknown asset balances require refresh.

Use the resulting demand for cargo fitting and Multibuy below. In whole-stack operations, consolidate
inputs with the bulk holder and hand over the whole pool in installer order; do not turn personal
empty hangars into separate material batches.

```bash
python .claude/skills/industry-jobs/job_board.py [--site NAME]   # idle copies, pool check, restock lines
eve-skills ledger note show                                       # the last jobs plan, if the installation keeps a ledger
```

- **Horizon** (choice; default 3 days). A trip covers what the slots and labs will use until the next
  trip, not only today's idle copies. Buying for exactly one round leaves the labs dry the next day.
- **Build lines:** `"PRODUCT=UNITS[:ME[:RUNS]][@CHAR]"`. Count the idle copies plus the copies the running
  and the planned invention will deliver (expected successes), capped by what the build slots can run
  over the horizon. Order the lines by priority (margin per job-hour, or ISK per m3 when the hold is the
  limit): the cargo fit fills them in that order.
- **Invention:** `--invent "T2 PRODUCT=ATTEMPTS[:DECRYPTOR][@CHAR]"`. Datacores come from the recipe; one
  decryptor per attempt only when named.
- **Buffers:** `--extra "TYPE=QTY"` buys the full quantity on top of stock.
- If existing copies plus running invention already cover more than the horizon, do not add fresh lab
  rounds or buffers on top: use what is coming first.

## 2. Run the planner

```bash
eve-skills restock --site NAME [--hub jita] [--hauler CHAR] [--cargo M3] [--days N] \
  --invent "T2 PRODUCT=ATTEMPTS:DECRYPTOR" --extra "TYPE=QTY" "PRODUCT=UNITS:ME:RUNS" ... [--json]
```

It nets the demand against stock on site (a corp site's hangars, or every site character's own hangar),
then what you own at the hub (pick-ups), then the hauler's other hangars (moves). Invention and extras
fit first, then lines in the order given; the last line that does not fit is cut. Output: lines covered,
a per-material table, total m3 and ISK, the **Multibuy** block, pick-ups, moves, transfers between
characters (personal sites), freight on the site's route, and what is left for a later trip.

- Materials are direct inputs only: a component you build yourself is its own line.
- Stock a character holds outside the site and outside the hauler's hangars is not seen: when a line is
  new, or someone may have bought something personally, check `inventory --char NAME --items` for each
  character before handing over the list.
- Plan from assets read **before** the install round the demand covers. Jobs installed after the asset
  snapshot already took their inputs, and the planner then counts that demand twice.

### Price check

`restock` judges every material against its 30-day normal and prints a price check, plus a second
Multibuy with the check applied when it changes anything:
- **dip**: extra units in proportion to the depth, capped at 14 days of use including stock held, and
  only from asks still under the dip line after the trip's own buy;
- **high**: buy only the need; leave buffers for a cheaper day;
- **falling**: a new level; nothing bought ahead.

`--days` must match the horizon the lines cover, or the per-day use (and the cap) is wrong. Always ask the
user about dip extras: list each with depth, extra units, ISK and m3, and give both Multibuy blocks.

## 3. Rules

- **Density first.** Small, valuable inputs (decryptors, datacores, components) gate the whole operation;
  bulky ones go on a separate trip when the list exceeds one hold. Say how many loads the full list is.
- **Book depth.** The ISK total prices everything at the lowest ask. For big buys check `market TYPE
  --hub HUB --depth 5` and quote the depth-weighted cost when the first order cannot cover the line.
- **Standing buy orders** (choice). A bid saves (ask - bid) / ask minus the broker fee, paid up front and
  not refunded. Bid only where that gap stays above a few percent, re-measure every visit, size bids for
  the next trip's horizon, and reprice them on each visit: only the issuing character can modify an
  order, and only from its region within its trading range. Check `orders --buy` before saying a bid exists.
- **Courier freight** (personal or remote sites): the site's `freight_in` route prices the haul per m3 plus
  collateral with a minimum per contract. Batch inputs so the minimum stays small. Compare the landed hub
  cost with buying from a local market (`market --hub KEY`) before choosing.
- **Did the last trip buy everything?** Compare the previous plan's Multibuy with the purchases since
  (ledger `transactions`, `is_buy = 1`) and name missing lines. Ask the user to confirm every line filled.
- Tell sell orders from buy orders: a bid is not a price you can buy at.
- Assets are cached about an hour: if jobs were just installed, the stock may still include their inputs.

## 4. Reply format

1. One line: total need vs cargo (m3, loads), ISK, the horizon it covers, and the first input that runs
   out and when.
2. The **Multibuy** paste in a code block, then `What | m3 | ISK | Why` (which lines or jobs it unlocks).
3. Pick-ups, moves, transfers between characters, and where things go on arrival.
4. What stays for a later trip.
5. If the installation keeps a ledger: a note with the plan; update the restock to-do.

## Where new rules go

Something true for any operation (how the planner nets stock, a market mechanic) belongs in this skill,
through a pull request; it must pass the private-terms check. This installation's horizon, hauler, routes,
standing bids, prices and dated findings go into its playbook: `eve-skills operation playbook supply-run
--path`.
