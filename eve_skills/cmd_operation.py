"""operation: show the configured sites, facilities, hubs and freight, or print a starting template."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

from . import operation, paths, render


def _pct(value: float | None) -> str:
    return "unknown" if value is None else f"{value * 100:g}%"


def _multipliers(values: dict[str, float]) -> str:
    return ", ".join(f"{k} {v:.4f}" for k, v in sorted(values.items())) or "none"


def operation_json(op: operation.Operation) -> dict:
    return {
        "file": op.path or operation.file_path(),
        "configured": op.configured,
        "default_site": op.default_site,
        "sites": {k: {"owner": s.owner, "facility": s.facility, "facilities": dict(s.facilities),
                      "characters": list(s.characters), "readers": list(s.readers), "hub": s.hub,
                      "freight_in": s.freight_in, "freight_out": s.freight_out,
                      "seller": s.seller, "pool_holder": s.pool_holder} for k, s in op.sites.items()},
        "facilities": {k: {"system": f.system, "label": f.label, "location_id": f.location_id, "tax": f.tax,
                           "material_multiplier": f.material, "time_multiplier": f.time,
                           "derived_from": f.derived_from, "unknown": list(f.unknown)}
                       for k, f in op.facilities.items()},
        "hubs": {k: {"label": h.label, "location_id": h.location_id, "system_id": h.system_id,
                     "region_id": h.region_id, "reader": h.reader} for k, h in op.hubs.items()},
        "freight": {k: {"per_m3": f.per_m3, "collateral_pct": f.collateral_pct, "min": f.minimum}
                    for k, f in op.freight.items()},
        "planning": {"timezone": op.planning.timezone, "finish_windows": op.planning.finish_windows,
                     "never_build": list(op.planning.never_build), "t1_copies": op.planning.t1_copies},
        "selling": {"keep": list(op.selling.keep)},
    }


def _site_facility_cell(site: operation.Site) -> str:
    """`labs; manufacturing: factory` - the default facility, then each activity that runs elsewhere."""
    parts = [site.facility] + [f"{activity}: {key}" for activity, key in site.facilities.items()]
    return "; ".join(parts)


def operation_text(op: operation.Operation) -> str:
    if not op.configured:
        return (f"no operation file at {operation.file_path()} - every command uses its built-in "
                f"defaults.\nStart from: eve-skills operation template > {operation.file_path()}")
    lines = [f"operation file {op.path}", ""]
    rows = [[k + (" (default)" if k == op.default_site else ""), s.owner, _site_facility_cell(s),
             ", ".join(s.characters if s.owner == "personal" else s.readers) or "-",
             s.seller or "-", s.pool_holder or "-"]
            for k, s in sorted(op.sites.items())]
    lines.append(render.table(["site", "owner", "facility", "characters / readers", "seller", "pool holder"],
                              rows))
    lines.append("")
    for key, f in sorted(op.facilities.items()):
        lines.append(f"facility {key}: " + (f"{f.label}, " if f.label else "") + f"{f.system or 'system ?'}"
                     + (f", location {f.location_id}" if f.location_id else ", location id not set")
                     + f", tax {_pct(f.tax) if f.tax is not None or f.unknown else 'NPC station rate'}")
        if f.derived_from:
            lines.append(f"  derived from {f.derived_from}")
        lines.append(f"  material multiplier: {_multipliers(f.material)}")
        lines.append(f"  time multiplier:     {_multipliers(f.time)}")
        if f.unknown:
            lines.append(f"  NOT SET: {', '.join(f.unknown)} - until filled in, a missing tax is the NPC rate and missing rigs add no bonus")
    if op.hubs:
        lines.append("")
        lines.append(render.table(["hub", "label", "location", "region", "reader"],
                                  [[k, h.label, str(h.location_id), str(h.region_id), h.reader or "-"]
                                   for k, h in sorted(op.hubs.items())]))
    if op.freight:
        lines.append("")
        lines.append(render.table(["freight route", "ISK/m3", "collateral", "minimum"],
                                  [[k, f"{f.per_m3:,.0f}", f"{f.collateral_pct:g}%", render.isk(f.minimum)]
                                   for k, f in sorted(op.freight.items())]))
    plan, sell = op.planning, op.selling
    lines.append("")
    lines.append(f"planning: time zone {plan.timezone or 'not set (UTC)'}; finish windows "
                 f"{plan.finish_windows or 'not set'}; T1 copies for {plan.t1_copies}; never build: "
                 f"{', '.join(plan.never_build) or 'nothing listed'}")
    lines.append(f"selling: keep {', '.join(sell.keep) if sell.keep else 'nothing listed (everything may be sold)'}")
    lines.append("")
    lines.append("engineering-complex job-cost reductions are not modelled; rig bonuses are assumed to "
                 "cover every product built there")
    return "\n".join(lines)


_PLAYBOOK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _stamp(path: str) -> str:
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")


def _first_heading(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    return line.lstrip("#").strip()
    except (OSError, UnicodeDecodeError):
        pass
    return "-"


def playbook_path(name: str) -> str:
    stem = name[:-3] if name.lower().endswith(".md") else name
    if not _PLAYBOOK_NAME.match(stem):
        raise RuntimeError(f"playbook name '{name}' must be a plain file name such as industry-jobs")
    return os.path.join(paths.playbook_dir(), f"{stem}.md")


def playbook_text(name: str | None, path_only: bool = False) -> str:
    """`operation playbook [NAME] [--path]`: list the playbook, print one file, or its path."""
    folder = paths.playbook_dir()
    if name is None:
        if path_only:
            return folder
        files = sorted(f for f in os.listdir(folder) if f.endswith(".md")) if os.path.isdir(folder) else []
        if not files:
            return (f"no playbook files in {folder} - create <skill>.md there (e.g. industry-jobs.md) for "
                    f"this installation's own rules")
        rows = []
        for entry in files:
            full = os.path.join(folder, entry)
            rows.append([entry[:-3], _first_heading(full), f"{os.path.getsize(full):,} B", _stamp(full)])
        return f"playbook {folder}\n\n" + render.table(["name", "first heading", "size", "modified"], rows)
    path = playbook_path(name)
    if path_only:
        return path
    if not os.path.isfile(path):
        return f"no playbook for {name} (create {path})"
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    return f"playbook {path} (modified {_stamp(path)})\n\n{body.rstrip()}"


def cmd_operation(args):
    if args.action == "playbook":
        print(playbook_text(getattr(args, "name", None), getattr(args, "path", False)))
        return
    if getattr(args, "name", None) or getattr(args, "path", False):
        raise RuntimeError("NAME and --path only apply to: eve-skills operation playbook")
    if args.action == "template":
        print(json.dumps(operation.TEMPLATE, indent=2))
        return
    op = operation.load()
    if args.json:
        print(json.dumps(operation_json(op), indent=2))
    else:
        print(operation_text(op))
