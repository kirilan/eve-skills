"""Finished goods held but not listed anywhere: where each stack sits, and what it would list for.

Usage (from the repo root; a source checkout runs `.venv/bin/python` instead of `python`):
    python .claude/skills/market-trip/unlisted_goods.py [--hub jita] [--seller NAME] [--corp-char NAME]
        [--from SYSTEM] [--include-t1] [--json]

Reads `ledger inventory` (held > 0, class finished), locates each stack in the assets of every site of
the operation file (corp sites through a reader, personal sites per character) and the seller's, and
prices it one tick under the hub's real floor (the bulk-sell-pricing rule, from `eve_skills.pricing`),
with a LOSS flag when the net listing is below the ledger unit cost. Items whose name contains an entry
of the operation file's `selling.keep` (materials the operation consumes) and T1 modules (inputs to T2
builds; `--include-t1` shows them) are left out. For open orders against the books use the CLI:
`orders --char NAME --check`.

The seller defaults to the default site's `seller` in the operation file, the corp reader to the corp
sites' first readers, and the origin of `sell-plan` to the first site's system; without an operation
file pass `--seller` and `--from` (`eve-skills operation template` prints a starting point).

One listing per item per hub: a stack whose item already has a sell order in the hub is flagged HOLD,
shows the open order count and units left, and stays out of the totals; list it once the open order has
sold out (several orders of one item only undercut each other). An open order with at most 10% of its
original quantity left is nearly sold out, and a second listing beside it keeps the sales going: such
stacks are flagged TOP-UP and counted.

Price state (`prices signal`, Jita): each stack carries its state against the 30-day normal. On a DIP
only the signal's sell share is brought (the rest stays in the hangar, capped at two weeks of sales),
unless the whole stack is held - then it may be listed once at normal. HIGH: list it all now. FALLING:
a new level, sell. The totals count what to bring, not everything held.
"""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys

from eve_skills import pricing

TAIL_PCT = 10  # an open order with at most this % of its quantity left does not block a second listing


def cli(*args: str) -> dict | None:
    """`eve-skills ARGS --json` as a dict; None when it fails. The script's only way to the CLI: tests
    replace it."""
    run = subprocess.run([sys.executable, "-m", "eve_skills", *args, "--json"], capture_output=True, text=True)
    if run.returncode or not run.stdout.strip():
        print(f"! eve-skills {' '.join(args[:3])} failed: {run.stderr.strip()[-300:]}", file=sys.stderr)
        return None
    return json.loads(run.stdout)


def books(names: list[str], hub: str, seller: str) -> dict[str, dict]:
    if not names:
        return {}
    data = cli("market", *names, "--hub", hub, "--depth", "30", "--history", "30", "--seller", seller)
    return {t["name"]: t["scopes"][0] for t in (data or {}).get("types", [])}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seller", help="character who lists the goods (default: the default site's seller)")
    ap.add_argument("--corp-char", default=None,
                    help="reader for corp orders and assets (default: the corp sites' first readers)")
    ap.add_argument("--from", dest="origin", help="system the goods leave from for sell-plan "
                                                 "(default: the first site's system)")
    ap.add_argument("--hub", default="jita", help="hub to price unlisted goods for")
    ap.add_argument("--include-t1", action="store_true",
                    help="also list held T1 modules (left out by default: T2 module builds consume them)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    # Where finished goods sit: every corp site (read through a reader) and every personal site's
    # characters (their own hangars), from the operation file.
    op = cli("operation") or {}
    site_map = op.get("sites") or {}
    sites = list(site_map.values())
    default = site_map.get(op.get("default_site") or "") or (sites[0] if len(sites) == 1 else {})
    seller = args.seller or default.get("seller") or next((s["seller"] for s in sites if s.get("seller")), None)
    corp_readers = [args.corp_char] if args.corp_char else [
        s["readers"][0] for s in sites if s["owner"] == "corp" and s.get("readers")]
    personal_chars = [c for s in sites if s["owner"] == "personal" for c in s.get("characters") or []]
    origin = args.origin or next((((op.get("facilities") or {}).get(s["facility"]) or {}).get("system")
                                  for s in sites if ((op.get("facilities") or {}).get(s["facility"]) or {}).get("system")),
                                 None)
    if not seller or not origin:
        sys.exit("unlisted_goods needs " + " and ".join(
            what for what, ok in (("a seller (--seller, or sites.<site>.seller in the operation file)", seller),
                                  ("an origin system (--from, or a site in the operation file)", origin)) if not ok)
            + "; `eve-skills operation template` prints a starting point")
    keep = tuple(k.casefold() for k in (op.get("selling") or {}).get("keep") or ())

    ours: set[int] = set()
    open_here: dict[str, list[tuple[int, int]]] = collections.defaultdict(list)  # type -> (left, total) per open sell order in the hub
    for extra in ([], ["--corp"]):
        d = cli("orders", "--char", seller, *extra)
        for o in (d or {}).get("orders", []):
            if o["order_id"] in ours:
                continue
            ours.add(o["order_id"])
            if not o["is_buy"] and o["location_name"].lower().startswith(args.hub.lower()):
                open_here[o["type_name"]].append((o["volume_remain"], o["volume_total"]))
    signal = cli("prices", "signal", "--all") or {}
    states = {r["type"]: r for r in signal.get("items", []) if r.get("type")}
    report = {"unlisted": [], "prices_synced": signal.get("prices_synced"), "seller": seller,
              "keep": list((op.get("selling") or {}).get("keep") or [])}
    if True:
        inv = cli("ledger", "inventory", "--all")
        held = {r["type_name"]: r for r in (inv or {}).get("items", [])
                if r["class"] == "finished" and r["held"] > 0 and not any(k in r["type_name"].casefold() for k in keep)
                and (args.include_t1 or not r["type_name"].endswith(" I"))}
        where = collections.defaultdict(collections.Counter)
        m3 = {}
        sources = ([["inventory", "--corp", "--char", r, "--items"] for r in corp_readers] +
                   [["inventory", "--char", c, "--items"] for c in dict.fromkeys(personal_chars + [seller])])
        for src in sources:
            d = cli(*src)
            for it in (d or {}).get("characters", [{}])[0].get("items", []):
                if it["type_name"] in held:
                    path = it.get("location_path") or [{}]
                    loc = path[0].get("name", "?")
                    div = it.get("division")
                    who = "" if "--corp" in src else f" ({src[src.index('--char') + 1]})"
                    where[it["type_name"]][f"{loc}{' ' + div if div else ''}{who}"] += it["quantity"]
        sp = None
        if held:
            cmd = ["sell-plan", "--seller", seller, "--from", origin, "--hub", args.hub]
            for n, r in held.items():
                cmd += ["--item", f"{n}={r['held']}"]
            sp = cli(*cmd)
        for it in (sp or {}).get("items", []):
            m3[it["type"]] = it.get("packaged_m3")
        book = books(sorted(held), args.hub, seller)
        for n, r in sorted(held.items(), key=lambda kv: -(kv[1]["market_value"] or 0)):
            s = book.get(n)
            suggest = net = None
            if s:
                comp = [x for x in s["depth"]["sell"] if x["order_id"] not in ours]
                per_day = (s.get("history") or {}).get("volume_per_day") or 0
                if comp:
                    i, _ = pricing.real_floor(comp, per_day, r["held"])
                    suggest = pricing.tick_below(comp[i]["price"])
                    fee = (s.get("sales_tax_pct", 4.2) + s.get("broker_fee_pct", 1.8)) / 100
                    net = suggest * (1 - fee)
            sig = states.get(n) or {}
            bring = r["held"]
            if sig.get("state") == "dip":
                # Same rule as bulk-sell-pricing: keep up to the cap (two weeks of our sales) of this stack.
                cap = sig.get("hold_cap")
                bring = 0 if cap is None else r["held"] - min(r["held"], int(cap))
            report["unlisted"].append(dict(
                state=sig.get("state"), vs_normal_pct=sig.get("deviation_pct"), normal=sig.get("normal"),
                bring=bring, signal_reason=sig.get("reason"),
                type=n, held=r["held"], listed=r["in_sell_orders"], unit_cost=r["unit_cost"],
                suggest=suggest, net=net, loss=(net is not None and r["unit_cost"] and net < r["unit_cost"]),
                m3=m3.get(n), where=dict(where.get(n, {})),
                open_orders=len(open_here.get(n, [])), open_left=sum(left for left, _ in open_here.get(n, [])),
                open_total=sum(total for _, total in open_here.get(n, [])),
                blocking_orders=sum(1 for left, total in open_here.get(n, []) if left * 100 > total * TAIL_PCT)))

    if args.json:
        print(json.dumps(report, indent=1))
        return
    print("kept back (selling.keep): " + (", ".join(report["keep"]) or
                                          "nothing - selling.keep is not set in the operation file"))
    if report["unlisted"]:
        print(f"\n## Finished goods held but not listed ({args.hub} price = one tick under the real floor)")
        tot_m3 = tot_val = 0
        for r in report["unlisted"]:
            m = (r["m3"] or 0)
            hold = r["blocking_orders"] > 0
            share = r["bring"] / r["held"] if r["held"] else 0
            if not hold:
                tot_m3 += m * share
                tot_val += (r["suggest"] or 0) * r["bring"]
            locs = "; ".join(f"{k}: {v}" for k, v in r["where"].items()) or "location not found (in transit/container?)"
            sug = f"{r['suggest']:,.0f}" if r["suggest"] else "-"
            flag = "  LOSS vs cost" if r["loss"] else ""
            if hold:
                flag += f"  HOLD: already {r['open_orders']} {args.hub} order(s), {r['open_left']} left"
            elif r["open_orders"]:
                flag += f"  TOP-UP: open {args.hub} order nearly sold out ({r['open_left']}/{r['open_total']} left), list beside it"
            if r["state"]:
                dev = f" {r['vs_normal_pct']:+.0f}%" if r["vs_normal_pct"] is not None else ""
                flag += f"  {r['state'].upper()}{dev}"
                if r["state"] == "dip" and not hold:
                    flag += (f": bring {r['bring']}, keep {r['held'] - r['bring']}" if r["bring"] < r["held"]
                             else f": bring all, or list all at normal {r['normal']:,.0f}")
            print(f"  {r['type'][:38]:38} {r['held']:>6} held  {m:7.0f} m3  cost {r['unit_cost']/1e6:6.2f}M  {args.hub} {sug:>12}{flag}")
            print(f"      at {locs}")
        print(f"  total to list ~{tot_m3:,.0f} m3, ~{tot_val/1e9:.2f}B at suggested prices "
              f"(HOLD stacks and dip shares kept back excluded)")
        print(f"  price states from prices signal, synced {report['prices_synced'] or 'never'}")
    if not report["unlisted"]:
        print("every finished good held is listed")


if __name__ == "__main__":
    main()
