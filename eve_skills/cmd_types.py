"""`types`: what an item is - id, exact name, group, category and packaged volume - for names or ids.

The look-up sessions kept doing by hand before a market or haul question: which type id a name is, and
how many m3 a packaged unit takes. Names are matched exactly (case aside), as everywhere else in this
tool, through one batched `/universe/ids` call; group, category and volume come from the disk-cached type
catalogue (`universe.type_info`). `-` reads names from stdin, one per line, and accepts the in-game paste
format `name<TAB>quantity`: the quantity is echoed and an m3 total added.
"""

from __future__ import annotations

import csv
import json
import sys

from . import esi as esi_mod, render, sso, universe

CSV_COLUMNS = ["input", "type_id", "name", "group", "category", "packaged_m3", "quantity", "total_m3"]
IDS_BATCH = 500          # /universe/ids takes at most 500 names per request


def parse_line(line: str) -> tuple[str, int | None] | None:
    """One stdin line -> (name or id, quantity or None); blank lines -> None.

    The in-game copy of a cargo or Multibuy list is `name<TAB>quantity` (quantities may carry thousands
    separators); anything without a tab is just a name."""
    text = line.strip("\r\n")
    if not text.strip():
        return None
    name, tab, rest = text.partition("\t")
    quantity = None
    if tab:
        cell = rest.split("\t")[0].strip().replace(",", "").replace(" ", "").replace(" ", "")
        if cell:
            try:
                quantity = int(cell)
            except ValueError:
                raise RuntimeError(f"quantity '{rest.strip()}' on line '{text.strip()}' is not a whole number") from None
    return name.strip(), quantity


def _inputs(args) -> list[tuple[str, int | None]]:
    items: list[tuple[str, int | None]] = []
    for arg in args.names:
        if arg == "-":
            items.extend(parsed for line in sys.stdin if (parsed := parse_line(line)) is not None)
        elif arg.strip():
            items.append((arg.strip(), None))
    if not items:
        raise RuntimeError("no names or ids given")
    return items


def resolve(client, specs: list[str]) -> dict[str, int]:
    """Spec -> type id for every spec that names a type: digits as ids, names through one batched
    `/universe/ids` lookup (exact match, case aside, in the `inventory_types` bucket only)."""
    found: dict[str, int] = {}
    names = []
    for spec in specs:
        if spec.isdigit():
            found[spec] = int(spec)
        elif spec.casefold() not in {name.casefold() for name in names}:
            names.append(spec)
    for start in range(0, len(names), IDS_BATCH):
        chunk = names[start:start + IDS_BATCH]
        hits = (client.post("/universe/ids", chunk) or {}).get("inventory_types") or []
        by_name = {str(hit.get("name", "")).casefold(): int(hit["id"]) for hit in hits if hit.get("id")}
        for spec in chunk:
            if spec.casefold() in by_name:
                found[spec] = by_name[spec.casefold()]
    for spec in specs:                       # a repeated name in another spelling of its case
        if spec not in found:
            match = next((ident for key, ident in found.items() if key.casefold() == spec.casefold()), None)
            if match is not None:
                found[spec] = match
    return found


def _known(info: universe.TypeInfo | None) -> bool:
    return info is not None and info.group_id is not None


def cmd_types(args):
    items = _inputs(args)
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    ids = resolve(client, [spec for spec, _qty in items])
    infos = universe.type_info(client, set(ids.values()))
    with_qty = any(qty is not None for _spec, qty in items)
    rows, unknown = [], []
    for spec, qty in items:
        info = infos.get(ids[spec]) if spec in ids else None
        if not _known(info):
            unknown.append(spec)
            continue
        volume = info.packaged_volume
        rows.append({"input": spec, "type_id": info.type_id, "name": info.name, "group": info.group_name,
                     "category": info.category_name, "packaged_m3": volume, "quantity": qty,
                     "total_m3": volume * qty if volume is not None and qty is not None else None})
    total = sum(row["total_m3"] for row in rows if row["total_m3"] is not None) if with_qty else None
    if args.json:
        print(json.dumps({"types": rows, "unknown": unknown, "total_m3": total}, indent=2))
    elif args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow(["" if row[key] is None else row[key] for key in CSV_COLUMNS])
        for spec in unknown:
            print(f"not a type: {spec}", file=sys.stderr)
    else:
        headers = ["type_id", "name", "group", "category", "m3"] + (["qty", "m3 total"] if with_qty else [])
        table = [[str(row["type_id"]), row["name"], row["group"], row["category"],
                  "?" if row["packaged_m3"] is None else f"{row['packaged_m3']:,.2f}"] +
                 (["" if row["quantity"] is None else f"{row['quantity']:,}",
                   "" if row["total_m3"] is None else f"{row['total_m3']:,.2f}"] if with_qty else [])
                 for row in rows]
        blocks = [render.table(headers, table)] if table else []
        if with_qty:
            blocks.append(f"total: {total:,.2f} m3 (packaged)")
        blocks.extend(f"not a type: {spec} (names must match exactly; try the numeric id)" for spec in unknown)
        print("\n".join(blocks))
    return 1 if unknown else 0
