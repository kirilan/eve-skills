"""Blueprint inventory and industry planning commands."""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass

from . import divisions, esi as esi_mod, exports, freshness, render, universe


@dataclass
class BlueprintOwner:
    name: str
    character_id: int
    corporation_id: int | None
    token: dict
    blueprints: list[dict]
    blueprint_meta: esi_mod.Meta
    jobs: list[dict]
    jobs_meta: esi_mod.Meta | None
    notes: list[str]
    division_names: dict[int, str]




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
            rows, meta = client.get_all_meta(
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
        owner = BlueprintOwner(owner_name, cid, corp_id, tok, list(rows), meta, [], None, [], {})
        if corp_id is not None:
            owner.division_names, note = divisions.fetch(client, tok, corp_id)
            if note:
                owner.notes.append(note)
            if "esi-industry.read_corporation_jobs.v1" in set(tok.get("scopes") or []):
                try:
                    owner.jobs, owner.jobs_meta = client.get_all_meta(
                        f"{_jobs_path(cid, corp_id)}?include_completed=true",
                        token=tok["access_token"],
                    )
                except esi_mod.AuthError:
                    pass
            count = freshness.delivered_after(owner.jobs, owner.blueprint_meta)
            warning = freshness.delivery_warning(count, "blueprint")
            if warning:
                owner.notes.append(f"warning: {warning}")
        owners.append(owner)
    return client, owners, hints + [f"warning: {line}" for line in failures]


def _apply_idle(client, owner: BlueprintOwner) -> None:
    required = ("esi-industry.read_corporation_jobs.v1" if owner.corporation_id is not None
                else "esi-industry.read_character_jobs.v1")
    granted = set(owner.token.get("scopes") or [])
    if required not in granted:
        owner.notes.append("--idle could not be applied: no jobs consent; run: eve-skills login --scopes jobs")
        return
    try:
        if owner.corporation_id is not None and owner.jobs_meta is not None:
            jobs = owner.jobs
        else:
            path = _jobs_path(owner.character_id, owner.corporation_id)
            if owner.corporation_id is not None:
                jobs, owner.jobs_meta = client.get_all_meta(
                    path, token=owner.token["access_token"])
            else:
                jobs, _meta = client.get_meta(path, token=owner.token["access_token"])
    except esi_mod.AuthError as err:
        owner.notes.append(f"--idle could not be applied: ESI refused the jobs document ({err})")
        return
    finished = {"cancelled", "delivered", "reverted"}
    busy = {int(job["blueprint_id"]) for job in jobs
            if job.get("blueprint_id") is not None and job.get("status") not in finished}
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
    wanted_division = divisions.resolve(division, owner.division_names)
    rows = []
    for bp in owner.blueprints:
        type_id = int(bp["type_id"])
        type_name = names.get(type_id) or f"type {type_id}"
        kind = _blueprint_kind(bp)
        flag = bp.get("location_flag") or "-"
        number = divisions.number(flag)
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
            "division": divisions.label(flag, owner.division_names),
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
    if args.division is not None and not args.corp:
        raise RuntimeError("--division is only meaningful with blueprints --corp")
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
                        "documents": ({
                            "blueprints": freshness.document(owner.blueprint_meta),
                            "jobs": (freshness.document(owner.jobs_meta)
                                     if owner.jobs_meta is not None else None),
                        } if owner.corporation_id is not None else None),
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
        for owner, _rows in documents:
            if owner.corporation_id is not None:
                print(freshness.line("corp blueprints", owner.blueprint_meta), file=sys.stderr)
                if owner.jobs_meta is not None:
                    print(freshness.line("corp jobs", owner.jobs_meta), file=sys.stderr)
        return

    blocks = list(all_notes)
    for owner, rows in documents:
        table_rows = [[row["type"], row["location"], row["division"], row["kind"],
                       str(row["runs"]), str(row["me"]), str(row["te"]), str(row["count"])]
                      for row in rows]
        table = render.table(["type", "location", "division", "kind", "runs", "ME", "TE", "count"],
                             table_rows) if table_rows else "(no blueprints)"
        cache = []
        if owner.corporation_id is not None:
            cache.append(freshness.line("corp blueprints", owner.blueprint_meta))
            if owner.jobs_meta is not None:
                cache.append(freshness.line("corp jobs", owner.jobs_meta))
        cache_text = ("\n" + "\n".join(cache)) if cache else ""
        blocks.append(f"{owner.name}{cache_text}\n{table}")
    print("\n\n".join(blocks))
