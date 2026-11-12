"""Price a batch of sell orders one tick under the real bottom of a hub's sell book.

Usage (from the repo root; a source checkout runs `.venv/bin/python` instead of `python`):
    python .claude/skills/bulk-sell-pricing/price_sell.py "Item Name=QTY" ... [--hub jita] [--seller NAME]
        [--thin-units 5] [--thin-pct 2] [--gap-pct 2] [--cost "Item Name=ISK" ...]
    python .claude/skills/bulk-sell-pricing/price_sell.py - < export.txt   # MultiSell "Export" text

Prints the text for the MultiSell window's "Import Prices..." button on stdout: one
`name<TAB>price` line per item, price with two decimals and a decimal point (7452000.00);
choose the decimal-point notation when importing. The reasoning goes to stderr. Order books come
from `eve-skills market --depth`. The seller (whose own orders are left out of the book) defaults to the
default site's `seller` in the operation file; without one, pass `--seller`.

Each item also gets its Jita price state from `prices signal` (Jita only): DIP (the stack would go up
under normal - say how much of it the signal holds back), HIGH (list it all now) or FALLING (a new
level). `--dip-at-normal` prices a DIP item at its 30-day normal instead of under the floor - for a
whole stack that is being held but listed once, to fill when the price comes back.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


# The rule itself lives in the CLI (`eve_skills/pricing.py`), shared with `orders --check` and
# `build-cost --sell-at`; these names are kept for callers of this script.
from eve_skills.pricing import real_floor as _real_floor, tick, tick_below  # noqa: E402


def cli(*args: str) -> dict | None:
    """`eve-skills ARGS --json` as a dict; None when it fails. The script's only way to the CLI: tests
    replace it."""
    run = subprocess.run([sys.executable, "-m", "eve_skills", *args, "--json"], capture_output=True, text=True)
    if run.returncode or not run.stdout.strip():
        return None
    return json.loads(run.stdout)


def default_seller() -> str | None:
    op = cli("operation") or {}
    sites = op.get("sites") or {}
    site = sites.get(op.get("default_site") or "") or (next(iter(sites.values())) if len(sites) == 1 else {})
    return site.get("seller") or next((s["seller"] for s in sites.values() if s.get("seller")), None)


def price_states(hub: str) -> dict[str, dict]:
    """`prices signal` rows by type name; empty off Jita or when the signal cannot be read."""
    if hub.lower() != "jita":
        return {}
    doc = cli("prices", "signal", "--all")
    if doc is None:
        print("warning: prices signal unavailable; no dip/high check", file=sys.stderr)
        return {}
    print(f"price states: prices synced {doc.get('prices_synced') or 'never'}", file=sys.stderr)
    return {r["type"]: r for r in doc.get("items", []) if r.get("type")}


def on_grid(price: float) -> float:
    """`price` rounded down to a valid EVE price (four significant digits)."""
    step = tick(price)
    return round(price // step * step, 2)


def real_floor(sells, per_day, thin_units, thin_pct, gap_pct, qty, stack_pct=10.0):
    """Index of the first real order and the sliver skipped before it (see eve_skills.pricing)."""
    return _real_floor(sells, per_day, qty, thin_units=thin_units, thin_pct=thin_pct,
                       stack_pct=stack_pct, gap_pct=gap_pct)


def parse_pairs(values: list[str], kind) -> dict[str, float]:
    out = {}
    for value in values:
        name, _, amount = value.rpartition("=")
        if not name or not amount:
            sys.exit(f"expected NAME=VALUE, got {value!r}")
        out[name.strip()] = kind(amount.replace(" ", "").replace(",", ""))
    return out


def own_order_ids(char: str) -> set[int]:
    """Open order ids of `char`, personal and corporation, so our own listings never count as the book."""
    ids: set[int] = set()
    for extra in ([], ["--corp"]):
        doc = cli("orders", "--char", char, *extra)
        if doc is None:
            print(f"warning: could not read {' '.join(extra) or 'personal'} orders of {char}; "
                  "our own orders may be undercut", file=sys.stderr)
            continue
        ids.update(o["order_id"] for o in doc["orders"])
    return ids


def qty_ratio(qty: int, per_day: float) -> float:
    return qty / per_day


def parse_export(text: str) -> dict[str, int]:
    """Quantities from a MultiSell export: type_id<TAB>name<TAB>qty<TAB>unit price<TAB>total."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        # Tabs from the client; runs of 2+ spaces when the paste went through a terminal or chat.
        fields = [f.strip() for f in re.split(r"\t| {2,}", line.strip())]
        if len(fields) < 3 or not fields[0].isdigit():
            continue  # header, totals or blank line
        qty = int("".join(ch for ch in fields[2] if ch.isdigit()) or 0)
        if qty:
            out[fields[1]] = out.get(fields[1], 0) + qty
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("items", nargs="+", help='"Exact Type Name=QTY", or - to read a MultiSell export on stdin')
    ap.add_argument("--hub", default="jita")
    ap.add_argument("--seller", help="stored character whose own (and corp) orders are left out of the book "
                                     "(default: the default site's seller in the operation file)")
    ap.add_argument("--thin-units", type=int, default=5)
    ap.add_argument("--thin-pct", type=float, default=2.0, help="percent of 30-day daily volume")
    ap.add_argument("--gap-pct", type=float, default=2.0)
    ap.add_argument("--stack-pct", type=float, default=10.0,
                    help="an ignored block may hold at most this percent of our own stack")
    ap.add_argument("--cost", action="append", default=[], help='"Exact Type Name=unit cost ISK"')
    ap.add_argument("--dip-at-normal", action="store_true",
                    help="price items in a Jita dip at their 30-day normal instead of under the floor "
                         "(a whole held stack listed once, to fill on the recovery)")
    args = ap.parse_args(argv)
    seller = args.seller or default_seller()
    if not seller:
        sys.exit("price_sell needs the seller whose own orders are left out of the book: pass --seller, or "
                 "set sites.<site>.seller in the operation file (`eve-skills operation template`)")

    items = parse_export(sys.stdin.read()) if args.items == ["-"] else parse_pairs(args.items, int)
    if not items:
        sys.exit("no items: pass \"Name=QTY\" arguments or a MultiSell export on stdin")
    costs = parse_pairs(args.cost, float)
    data = cli("market", *items, "--hub", args.hub, "--depth", "30", "--history", "30")
    if data is None:
        sys.exit("eve-skills market failed (check the exact type names)")
    ours = own_order_ids(seller)
    states = price_states(args.hub)

    paste = []
    for t in data["types"]:
        name = t["name"]
        scope = t["scopes"][0]
        own = [o for o in scope["depth"]["sell"] if o["order_id"] in ours]
        sells = [o for o in scope["depth"]["sell"] if o["order_id"] not in ours]
        per_day = (scope.get("history") or {}).get("volume_per_day") or 0
        avg = (scope.get("history") or {}).get("average_price")
        if not sells:
            print(f"{name}: no sell orders at {args.hub}; price it by hand", file=sys.stderr)
            continue
        i, skipped = real_floor(sells, per_day, args.thin_units, args.thin_pct, args.gap_pct,
                                items[name], args.stack_pct)
        floor = sells[i]
        price = tick_below(floor["price"])
        sig = states.get(name) or {}
        at_normal = args.dip_at_normal and sig.get("state") == "dip" and sig.get("normal")
        if at_normal:
            price = max(price, on_grid(sig["normal"]))
        paste.append(f"{name}\t{price:.2f}")
        note = ", ".join(f"{s['volume']}@{s['price']:,.0f}" for s in skipped) or "-"
        avg_text = f"; 30d avg {avg:,.0f}" if avg else ""
        print(f"{name}\n  book as of {scope['last_modified']}; {per_day:,.0f}/day{avg_text}", file=sys.stderr)
        print(f"  ignored: {note}", file=sys.stderr)
        if own:
            print("  ours (left out): " + ", ".join(f"{o['volume']}@{o['price']:,.0f}" for o in own),
                  file=sys.stderr)
        print(f"  real floor {floor['volume']}@{floor['price']:,.0f} -> list at {price:,.2f}", file=sys.stderr)
        if name in costs and price * 0.94 < costs[name]:
            print(f"  WARNING: ~{price * 0.94:,.0f} after ~6% tax+broker is below unit cost "
                  f"{costs[name]:,.0f}", file=sys.stderr)
        if avg and price > avg * 1.25:
            print("  NOTE: more than 25% over the 30-day average; the book has moved up or is thin",
                  file=sys.stderr)
        if per_day and qty_ratio(items[name], per_day) > 3:
            print(f"  NOTE: the stack is {qty_ratio(items[name], per_day):.0f} days of regional volume",
                  file=sys.stderr)
        state = sig.get("state")
        dev = f" {sig['deviation_pct']:+.0f}% vs normal {sig['normal']:,.0f}" if sig.get("deviation_pct") is not None else ""
        if state == "dip":
            # The cap (two weeks of our sales) applies to the stack being priced, whatever the ledger holds.
            cap = sig.get("hold_cap")
            kept = items[name] if cap is None else min(items[name], int(cap))
            print(f"  DIP{dev}: " + (
                "priced at normal (--dip-at-normal), fills when the price recovers" if at_normal else
                f"hold all {kept} in the hangar, or list the whole stack at normal with --dip-at-normal"
                if kept == items[name] else
                f"list {items[name] - kept} now and keep {kept} in the hangar (cap: two weeks of our sales)"),
                file=sys.stderr)
        elif state == "high":
            print(f"  HIGH{dev}: list it all now", file=sys.stderr)
        elif state == "falling":
            print(f"  FALLING{dev}: low for a week, a new level - waiting is unlikely to pay", file=sys.stderr)
        if avg and price < avg * 0.85:
            print("  WARNING: more than 15% under the 30-day average; check the book by hand",
                  file=sys.stderr)
    missing = set(items) - {t["name"] for t in data["types"]}
    for name in sorted(missing):
        print(f"{name}: not returned by market (check the exact type name)", file=sys.stderr)
    print("\n".join(paste))


if __name__ == "__main__":
    main()
