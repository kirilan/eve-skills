---
name: market-trip
description: Check an EVE Online operation's open sell and buy orders against each hub's book, find finished goods that are held but not listed, and plan a sell trip - what to bring, what to reprice, at what price, and whether the cargo fits. Works for any operation run with eve-skills. Use when the user asks "are our orders ok", "did my sell orders sell", "I can do a hub run, what should I bring", "orders to update", "check if I missed anything" after listing, or whether another hub is worth a trip.
---

# Market trip: orders, unlisted stock, sell run

Commands are written `eve-skills ...`. In a source checkout run `.venv/bin/python -m eve_skills ...` (and
`.venv/bin/python` for the scripts) from the repository root.

## 0. This installation first

```bash
eve-skills operation show                     # sites, each site's seller and hub, freight routes
eve-skills operation playbook market-trip     # this installation's own rules
```

**Playbook rules win over this skill's defaults.** With no playbook, use the defaults below and say which
choices you guessed (seller, hub, what to keep). The seller is the site's `seller`; items never offered
for sale are `selling.keep`.

## 1. Gather

```bash
eve-skills orders --char SELLER --check [--json]                       # every open order in its book
eve-skills ledger sync                                                  # the next script reads the ledger
python .claude/skills/market-trip/unlisted_goods.py [--hub jita] [--include-t1]
eve-skills prices signal [--hub KEY] [--json]                           # dip / high / falling / thin per type
```

- `orders --check` places each open order in its station's book, ours left out: status (`cheapest`,
  `undercut`, `behind sliver`, `outbid`, `top`, `alone`), best competing price, units ahead and the days
  of regional volume they are, and a suggested price (the bulk-sell-pricing rule). Undercut and outbid
  rows sort first. Add `--corp` for corporation orders other members issued.
- `unlisted_goods.py`: finished goods held but not listed (from `ledger inventory`), where each stack
  sits, m3, unit cost, the suggested price and a `LOSS` flag when the net listing is under cost. It reads
  every site of the operation file; its header says what `selling.keep` keeps back.
- `prices signal`: per watched type, the lowest competing ask against its 30-day normal and how many units
  to hold vs list. `--hub KEY` judges another hub (a site's own structure market, polled by `prices sync`
  for every site hub) against its region's normal; a type the region trades too rarely reads `THIN`.
  `unlisted_goods.py` prices for the default site's hub (`--hub` to choose), tags each stack `DIP` /
  `HIGH` / `FALLING` / `THIN` and, on a dip, says `bring N, keep M`. Check `prices synced` is recent (the
  installation's hourly `prices sync`).
- Also: `ledger inventory` (held vs listed), `orders --closed --char SELLER` (what filled),
  `sell-plan --seller SELLER --from SYSTEM --hub HUB --item "NAME=QTY"` (hub split).

Before proposing more manufacturing, run `production replenish --site NAME --hub KEY --days N`.
It uses own sales at this hub and combines live listed/unlisted stock, WIP, copies and expected invention.
A sold-out listing with stock held needs `list` or `haul` first. New lines without sales need an explicit
`--target "ITEM=QTY"` trial. Check timestamps and refresh actions before interpreting quantities.
Each destination is a separate scenario: allocate the shared stock only once across trips. Keep the
playbook's keep list and one-order policy when acting on its suggestions. See
[production workflow](../../../docs/production.md) for details.

## 2. Rules

- **Where to sell** (choice; default: the highest-volume hub). Most volume trades at the main hubs; list
  elsewhere only when `sell-plan` shows a real uplift within a day or two of that region's volume. Never
  leave valuable stock unlisted in a hangar for weeks.
- **Remote hubs need someone there.** Only the character who issued an order can change it, corporation
  orders included, and only from the order's region within its trading range. Orders nobody can reprice
  get undercut within hours. Cancel-and-relist pays the broker fee again; cancelling returns the goods to
  that station.
- **One listing per item per hub.** Several orders of one item only undercut each other. A stack whose
  item already has an open order in the hub is flagged `HOLD` and stays home; list it as one order when
  the open one has sold out, or reprice the open one. Exception: an open order with at most ~10% of its
  quantity left is nearly sold out, and a second listing beside it keeps sales going (`TOP-UP`). Check
  the `orders --check` rows for duplicates and tell the user.
- **Price state:**
  - `DIP`: bring and list only the `bring` quantity, keep the rest (at most about two weeks of sales). If
    the whole stack is held it may instead go up once at its normal price and fill on the recovery.
  - `HIGH`: list everything; these stacks go first when cargo is short.
  - `FALLING`: low for a week, a new level: sell, don't wait.
  - A `HOLD` stack stays home whatever its state. The edge from holding a dip is small: never skip a
    trip or leave stock unlisted for weeks only because of one.
- **Is a trip worth it?** Undercut by a few units that sell within a day is not; a gap of several percent
  on a stack worth days of volume is. Say which orders justify it.
- **Pricing:** use the bulk-sell-pricing skill for the final paste and re-run it in the hub just before
  listing: books refresh every 5 minutes.
- Don't sell what the operation consumes (`selling.keep`).
- **Cargo:** sum the packaged m3 of what is brought against the hold; from a remote site, compare the
  courier route (`freight_out`: per m3 plus collateral) with hauling yourself.
- **After the user lists:** character orders are cached about 20 minutes. Say so, and re-check after that
  rather than reporting missing orders as mistakes.

## 3. Reply format

1. One line: how many orders are undercut, the value unlisted, whether a trip is warranted.
2. Orders to reprice, by hub: `Item | Ours | Best other | Units ahead (days) | New price`.
3. What to bring: `Item | Qty | From | m3 | Suggested price | Cost | State`, total m3 vs cargo; flag losses;
   bring/keep for a dip. Full item names (or a contiguous part) so they paste into the in-game search.
4. Data timestamps (books, orders cache, prices synced).
5. After the trip: `ledger sync` and a note, if the installation keeps a ledger.

## Where new rules go

Something true for any operation (an order mechanic, how to read `orders --check`) belongs in this skill,
through a pull request; it must pass the private-terms check. This installation's seller, hubs, routes,
experiments and dated findings go into its playbook: `eve-skills operation playbook market-trip --path`.
