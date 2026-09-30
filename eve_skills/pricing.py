"""Order pricing rules: EVE's price ticks, the real floor of a sell book, and an order's standing.

Pure functions over book rows shaped like `market._order_depth` output (`order_id`, `price`,
`volume`), so `orders --check` and the bulk sell-pricing skill apply one rule.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

# A leading block of the sell book is a "sliver" - ignored rather than undercut - when it is thin on
# the market and next to our own stack, and clearly cheaper than the order after it. Undercutting a
# one-unit bait order gives away the gap on every unit we list; ignoring it only costs the minutes
# that unit takes to sell. Defaults from the Jita pricing rounds of 2026-09-27.
THIN_UNITS = 5          # a sliver may hold this many units ...
THIN_PCT = 2.0          # ... or this percent of the regional daily volume, whichever is more
STACK_PCT = 10.0        # but never more than this percent of the stack being priced
GAP_PCT = 2.0           # and the next order must sit more than this percent above it


def tick(price: float) -> float:
    """EVE's price step at `price`: prices carry at most four significant digits, 0.01 minimum."""
    if price <= 0:
        return 0.01
    return max(0.01, 10 ** (math.floor(math.log10(price)) - 3))


def tick_below(price: float) -> float:
    """The next valid price under `price` (3,325,000 -> 3,324,000; 1,000,000 -> 999,000; 912.1 -> 912.0)."""
    # Crossing a power of ten lands on the finer grid below it, which is still a valid price.
    return max(0.01, round(price - tick(price), 2))


def tick_above(price: float) -> float:
    """The next valid price over `price` (1,149,000 -> 1,150,000; 999,900 -> 1,000,000)."""
    return round(price + tick(price), 2)


def real_floor(sells: Sequence[dict], per_day: float, qty: int, *, thin_units: int = THIN_UNITS,
               thin_pct: float = THIN_PCT, stack_pct: float = STACK_PCT,
               gap_pct: float = GAP_PCT) -> tuple[int, list[dict]]:
    """Index of the first real order in a cheapest-first sell book, and the sliver skipped before it.

    A leading block is skipped when its volume is at most max(thin_units, thin_pct% of the daily
    volume) and at most stack_pct% of our `qty` (at least one unit), and the order after it is more
    than gap_pct% above the block's highest price. The longest such block wins, so two stray cheap
    units in a row are both ignored. Our own orders must already be left out of `sells`."""
    thin_cap = min(max(thin_units, per_day * thin_pct / 100), max(1, qty * stack_pct / 100))
    best = 0
    volume = 0
    for k in range(1, len(sells)):
        volume += sells[k - 1]["volume"]
        if volume > thin_cap:
            break
        if sells[k]["price"] > sells[k - 1]["price"] * (1 + gap_pct / 100):
            best = k
    return best, list(sells[:best])


def sell_price(sells: Sequence[dict], per_day: float, qty: int, **rule) -> tuple[float | None, list[dict]]:
    """One tick under the real floor of a competitor-only sell book, with the sliver it ignored."""
    if not sells:
        return None, []
    i, skipped = real_floor(sells, per_day, qty, **rule)
    return tick_below(sells[i]["price"]), skipped


def standing(is_buy: bool, price: float, remain: int, book: Sequence[dict],
             own_ids: Iterable[int], per_day: float | None) -> dict:
    """Where one of our orders stands in its station's book (that side only, best first).

    Returns `status` - sell: cheapest / undercut / behind sliver / alone; buy: top / outbid / alone -
    plus the best competing price, the competing units priced ahead of ours and how many days of
    regional volume they are, and `suggest`: a new price when a reprice would move us to the front
    (None when we are already there, or only a sliver is ahead of a sell order)."""
    own = set(own_ids)
    others = [row for row in book if row.get("order_id") not in own]
    per_day = per_day or 0.0
    if not others:
        return {"status": "alone", "best_other": None, "units_ahead": 0, "days_ahead": 0.0 if per_day else None,
                "suggest": None, "ignored": []}
    ahead = [row for row in others if (row["price"] > price if is_buy else row["price"] < price)]
    units = sum(row["volume"] for row in ahead)
    doc = {"best_other": others[0]["price"], "units_ahead": units,
           "days_ahead": units / per_day if per_day else None, "ignored": []}
    if is_buy:
        doc.update(status="outbid" if ahead else "top",
                   suggest=tick_above(others[0]["price"]) if ahead else None)
        return doc
    target, skipped = sell_price(others, per_day, remain)
    if not ahead:
        doc.update(status="cheapest", suggest=None)
    elif target is not None and target < price:
        doc.update(status="undercut", suggest=target, ignored=skipped)
    else:  # only a thin sliver is below us: leave the order where it is
        doc.update(status="behind sliver", suggest=None, ignored=skipped)
    return doc
