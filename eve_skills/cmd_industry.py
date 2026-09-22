"""Blueprint inventory and industry planning commands."""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass

from . import esi as esi_mod, exports, render, sso, universe


@dataclass
class BlueprintOwner:
    name: str
    character_id: int
    corporation_id: int | None
    token: dict
    blueprints: list[dict]
    notes: list[str]


def division_number(flag: str | None) -> int | None:
    """The numbered corporation hangar represented by a CorpSAG location flag."""
    text = flag or ""
    if text.startswith("CorpSAG") and text[7:].isdigit():
        number = int(text[7:])
        return number if 1 <= number <= 7 else None
    return None


def division_label(flag: str | None, names: dict[int, str] | None = None) -> str:
    """A player-facing hangar label, with a stable numbered fallback."""
    number = division_number(flag)
    if number is None:
        return flag or "-"
    return (names or {}).get(number) or f"division {number}"


def _blueprint_path(character_id: int, corporation_id: int | None) -> str:
    return (f"/corporations/{corporation_id}/blueprints" if corporation_id is not None
            else f"/characters/{character_id}/blueprints")


def _jobs_path(character_id: int, corporation_id: int | None) -> str:
    return (f"/corporations/{corporation_id}/industry/jobs" if corporation_id is not None
            else f"/characters/{character_id}/industry/jobs")


def _fetch_blueprint_owners(args) -> tuple[esi_mod.Esi, list[BlueprintOwner], list[str]]:
    scope = ("esi-corporations.read_blueprints.v1" if args.corp
             else "esi-characters.read_blueprints.v1")
    client, chars, hints = exports.targets(args, [("blueprints", scope)])
    owners: list[BlueprintOwner] = []
    failures: list[str] = []
    seen_corps: set[int] = set()
    for tok, public in chars:
        cid = int(tok["character_id"])
        cname = public.get("name") or tok.get("character_name") or str(cid)
        corp_id = exports.corp_of(public) if args.corp else None
        if args.corp:
            if corp_id is None:
                failures.append(f"{cname}: no corporation id on the public record")
                continue
            if corp_id in seen_corps:
                continue
        try:
            rows, _meta = client.get_all_meta(
                _blueprint_path(cid, corp_id), token=tok["access_token"]
            )
        except esi_mod.AuthError as err:
            why = ("corporation blueprints need the Director role for that corporation"
                   if args.corp else "the blueprints consent may no longer be granted")
            failures.append(f"{cname}: ESI refused ({err}) - {why}")
            continue
        if corp_id is not None:
            seen_corps.add(corp_id)
        owner_name = f"Corporation {corp_id} (read by {cname})" if corp_id is not None else cname
        owners.append(BlueprintOwner(owner_name, cid, corp_id, tok, list(rows), []))
    return client, owners, hints + [f"warning: {line}" for line in failures]


def _apply_idle(client, owner: BlueprintOwner) -> None:
    required = ("esi-industry.read_corporation_jobs.v1" if owner.corporation_id is not None
                else "esi-industry.read_character_jobs.v1")
    granted = set(owner.token.get("scopes") or [])
    if required not in granted:
        owner.notes.append("--idle could not be applied: no jobs consent; run: eve-skills login --scopes jobs")
        return
    try:
        path = _jobs_path(owner.character_id, owner.corporation_id)
        jobs = (client.get_all(path, token=owner.token["access_token"])
                if owner.corporation_id is not None else
                client.get(path, token=owner.token["access_token"]))
    except esi_mod.AuthError as err:
        owner.notes.append(f"--idle could not be applied: ESI refused the jobs document ({err})")
        return
    busy = {int(job["blueprint_id"]) for job in jobs if job.get("blueprint_id") is not None}
    owner.blueprints = [row for row in owner.blueprints
                        if int(row.get("item_id", 0)) not in busy]


def _blueprint_kind(row: dict) -> str:
    return "BPC" if int(row.get("quantity", 0)) == -2 else "BPO"


def _row_count(row: dict) -> int:
    quantity = int(row.get("quantity", 1))
    return quantity if quantity > 0 else 1


def _normalized_rows(client, owner: BlueprintOwner, args) -> list[dict]:
    type_ids = {int(row["type_id"]) for row in owner.blueprints if row.get("type_id") is not None}
    names = esi_mod.resolve_names(client, type_ids) if type_ids else {}
    # Blueprint payloads omit location_type. NPC station ids are int32; item/structure ids are not.
    location_rows = [dict(row, location_type=("station" if int(row.get("location_id", 0)) <=
                                                     esi_mod.INT32_MAX else "other"))
                     for row in owner.blueprints]
    places = universe.resolve_locations(
        client, location_rows, token=owner.token["access_token"],
        corporation_id=owner.corporation_id,
        character_id=None if owner.corporation_id is not None else owner.character_id,
    )
    query = (args.type or "").strip().casefold()
    division = getattr(args, "division", None)
    wanted_division = int(division) if division and str(division).isdigit() else None
    rows = []
    for bp in owner.blueprints:
        type_id = int(bp["type_id"])
        type_name = names.get(type_id) or f"type {type_id}"
        kind = _blueprint_kind(bp)
        flag = bp.get("location_flag") or "-"
        number = division_number(flag)
        if args.copies and kind != "BPC":
            continue
        if args.originals and kind != "BPO":
            continue
        if query and query not in type_name.casefold():
            continue
        if division is not None and (wanted_division is None or number != wanted_division):
            continue
        place = places.get(int(bp["location_id"]))
        rows.append({
            "item_id": int(bp["item_id"]),
            "type_id": type_id,
            "type": type_name,
            "location_id": int(bp["location_id"]),
            "location": place.name if place else f"location {bp['location_id']}",
            "flag": flag,
            "division": division_label(flag),
            "kind": kind,
            "runs": int(bp.get("runs", -1)),
            "me": int(bp.get("material_efficiency", 0)),
            "te": int(bp.get("time_efficiency", 0)),
            "count": _row_count(bp),
        })
    return rows


def _group_blueprints(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = (row["type_id"], row["location_id"], row["flag"], row["kind"],
               row["runs"], row["me"], row["te"])
        if key not in grouped:
            grouped[key] = dict(row, item_id=None, count=0)
        grouped[key]["count"] += row["count"]
    return list(grouped.values())


def _sort_blueprints(rows: list[dict], group_by: str) -> list[dict]:
    if group_by == "division":
        return sorted(rows, key=lambda row: (row["division"].casefold(), row["type"].casefold(),
                                             row["runs"], row["me"], row["te"]))
    return sorted(rows, key=lambda row: (row["type"].casefold(), row["division"].casefold(),
                                         row["runs"], row["me"], row["te"]))


def cmd_blueprints(args):
    """List owned blueprint originals and copies from ESI's blueprint endpoints."""
    client, owners, notices = _fetch_blueprint_owners(args)
    if args.idle:
        for owner in owners:
            _apply_idle(client, owner)
    documents = []
    for owner in owners:
        rows = _normalized_rows(client, owner, args)
        if not args.items:
            rows = _group_blueprints(rows)
        rows = _sort_blueprints(rows, args.group_by)
        documents.append((owner, rows))

    all_notes = notices + [note for owner, _rows in documents for note in owner.notes]
    if args.json:
        print(json.dumps({
            "owner_kind": "corporation" if args.corp else "character",
            "idle_requested": bool(args.idle),
            "grouped_by": args.group_by,
            "hints": [note for note in all_notes if not note.startswith("warning:")],
            "warnings": [note.removeprefix("warning: ") for note in all_notes
                         if note.startswith("warning:")],
            "owners": [{"name": owner.name, "character_id": owner.character_id,
                        "corporation_id": owner.corporation_id, "notes": owner.notes,
                        "blueprints": rows} for owner, rows in documents],
        }, indent=2))
        return
    if args.csv:
        columns = (["owner", "item_id", "type_id", "type", "location_id", "location", "flag",
                    "division", "kind", "runs", "me", "te", "count"] if args.items else
                   ["owner", "type_id", "type", "location_id", "location", "flag", "division",
                    "kind", "runs", "me", "te", "count"])
        writer = csv.DictWriter(sys.stdout, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for owner, rows in documents:
            for row in rows:
                doc = dict(row, owner=owner.name)
                writer.writerow({key: doc.get(key, "") for key in columns})
        for note in all_notes:
            print(note, file=sys.stderr)
        return

    blocks = list(all_notes)
    for owner, rows in documents:
        table_rows = [[row["type"], row["location"], row["division"], row["kind"],
                       str(row["runs"]), str(row["me"]), str(row["te"]), str(row["count"])]
                      for row in rows]
        table = render.table(["type", "location", "division", "kind", "runs", "ME", "TE", "count"],
                             table_rows) if table_rows else "(no blueprints)"
        blocks.append(f"{owner.name}\n{table}")
    print("\n\n".join(blocks))
