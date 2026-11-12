---
name: bulk-sell-pricing
description: Price a batch of EVE Online sell orders for a trade hub (default Jita 4-4) so each is the cheapest real offer, ignoring a thin sliver of very cheap stock at the bottom of the book, and produce the text for the MultiSell window's Import Prices button (name<TAB>price). Works for any seller set up in eve-skills. Use when the user asks for sell prices, a multi-sell or paste string, pastes a MultiSell export, or asks how to list or reprice goods at a hub.
---

# Bulk sell pricing

Goal: list every item one tick under the **real** bottom of the hub's sell book, not under a stray cheap
order. Undercutting a one-unit bait order gives away ISK on the whole stack for nothing: that unit sells
within minutes anyway.

Commands are written `eve-skills ...`; the script runs as `python .claude/skills/bulk-sell-pricing/
price_sell.py` (`.venv/bin/python` in a source checkout) from the repository root.

## 0. This installation first

```bash
eve-skills operation playbook bulk-sell-pricing   # this installation's own rules
```

**Playbook rules win over this skill's defaults.** The seller (whose own orders are left out of the book)
is the default site's `seller` in the operation file, or `--seller NAME`.

## The rule

1. Read the hub's sell book (station level) and the region's 30-day daily volume. **Leave our own orders
   out** (the seller's personal and corporation orders): the script drops them by order id, so a reprice
   never steps under our own listing.
2. **Ignore the leading cheap block** when all three hold:
   - thin on the market: its volume is at most `max(5 units, 2% of daily volume)`;
   - thin next to **our stack**: at most 10% of the quantity we are selling. Skipping a block only costs
     the time it takes to sell; undercutting it costs the gap on every unit we list;
   - clearly cheaper: the next order is more than **2%** above the block's highest price.
   Take the longest such block, so two stray cheap units in a row are both ignored. A single unit only a
   tick below the next order is the market: just undercut it. A thick block is the market too. Don't gate
   on the 30-day average: some books (salvage, for one) move far from it within days.
3. **List one tick below the first real order.** EVE prices carry 4 significant digits: 3,325,000 →
   3,324,000; 13,210,000 → 13,200,000; 1,000,000 → 999,000; 912.1 → 912.0.
4. **Sanity checks before handing it over:**
   - Price after sales tax and broker fee (`market --seller NAME` shows the net listing) against the unit
     cost (`ledger inventory` / `ledger products`). Flag any loss.
   - The script notes a price more than 25% over or 15% under the 30-day average, and a stack worth more
     than 3 days of regional volume. Mention these; they don't change the price.
   - **Price state** (Jita, from `prices signal`; the script prints it per item): `HIGH` - list it all
     now; `FALLING` - a new level, list at the floor anyway; `DIP` - the script says how many to keep in
     the hangar (up to about two weeks of sales) and how many to list now (tell the user to lower the
     MultiSell quantity), or `--dip-at-normal` prices a whole held stack once at its normal price so it
     fills on the recovery. One order per item per hub either way.
   - Don't sell what the operation consumes (`selling.keep`): take those home instead.

Worked example: the book is 1 @ 2,214,000, then 20 @ 2,926,000, 48 @ 2,927,000, 159 @ 2,929,000, at 138 a
day, and we sell 20. The single unit is thin (1 ≤ 5, ≤ 10% of 20) and 24% below the next order: ignore it
and list at **2,925,000**, not 2,213,000 (about 14M more over 20 units). Bonus: buy the stray unit and
relist it with ours.

## In-game round trip

1. In the hub station, select the items in the hangar, right-click > **Sell**. The MultiSell window opens.
2. Click **Export** (top right). It copies one line per item:
   `type_id<TAB>name<TAB>qty<TAB>unit price<TAB>total`. Its prices are the client's defaults (the best
   buy order). The user pastes this export to us.
3. We reply with the **import** text: one `name<TAB>price` line per item, price with two decimals and a
   decimal point, no thousands separators, no quantity, no type id, no header:
   `Hobgoblin II	512300.00`
4. The user copies it, clicks **Import Prices...** (top left of the MultiSell window), chooses the
   **decimal point** notation, and submits.

**Every row left in the window gets sold, at its default price when the import has no line for it.**
Import Prices only overwrites the rows it names, and the default is the best buy order - often far under
the real floor. So never leave a pasted item out of the import silently: for an item to keep home, put
"remove these rows before you click Sell" above the code block and still give it a line at the floor
price. A wrongly opened second order at the right price costs far less than a sale at the buy price.

**Outside the main hubs the default can be a lowball buy order** (a few ISK), and a row the import did not
overwrite then sells into it at once. Before handing over a paste for such a hub, check the best buy per
item (`market TYPE --hub HUB`) and say so above the code block when it is far below our price. Tell the
user to check every row's price column against the paste before clicking Sell.

The export format pasted back does not import, and neither does `name qty price`. If the import fails,
ask which notation the user chose and try `7,452,000.00` (with separators) next.

## How to run it

```bash
python .claude/skills/bulk-sell-pricing/price_sell.py - < export.txt      # or a heredoc of the paste
python .claude/skills/bulk-sell-pricing/price_sell.py "Hobgoblin II=200" "Damage Control II=40" \
  --cost "Hobgoblin II=420000"                                              # optional loss check
# --hub amarr|dodixie|rens|hek|KEY  --seller NAME  --thin-units 5  --thin-pct 2  --stack-pct 10  --gap-pct 2
# --dip-at-normal   price items in a Jita dip at their 30-day normal (a whole held stack, listed once)
```

Stdout is the import text. Stderr shows, per item, the book time, daily volume, 30-day average, what was
ignored, our own orders left out, the real floor and warnings. Use exact type names (the export has them).

## Reply format

- The import text in a code block, plus the reminder: Import Prices... > decimal point.
- A short table: item, what was ignored (if anything), real lowest order, our price, unit cost, price state
  (DIP/HIGH/FALLING; blank when normal) with the quantity to list now for a DIP.
- The book timestamp. Books refresh every 5 minutes and get undercut: re-run just before listing.
- Orders can only be placed or repriced from the hub's region, within the character's trading range, and
  new listings need the goods in the hub station.

## Where new rules go

A general pricing or client mechanic belongs in this skill, through a pull request; it must pass the
private-terms check. This installation's seller, examples and incidents go into its playbook:
`eve-skills operation playbook bulk-sell-pricing --path`.
