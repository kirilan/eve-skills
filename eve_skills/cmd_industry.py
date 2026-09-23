"""Blueprint inventory and industry planning commands."""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass

from . import alphadata, divisions, esi as esi_mod, exports, freshness, industry, render, sso, universe
from .industry_status import cmd_status


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


def _can_build_builders(client, specs: str | None):
    records = sso.list_characters()
    wanted = {part.strip().casefold() for part in (specs or "").split(",") if part.strip()}
    selected, matched = [], set()
    for record in records:
        name = record.get("character_name") or str(record["character_id"])
        keys = {name.casefold(), str(record["character_id"])}
        if wanted and not wanted.intersection(keys):
            continue
        matched.update(wanted.intersection(keys))
        token = sso.get_access_token(int(record["character_id"]))
        levels = None
        if "esi-skills.read_skills.v1" in set(token.get("scopes") or []):
            try:
                doc = client.get(f"/characters/{record['character_id']}/skills",
                                 token=token["access_token"])
                levels = {int(row["skill_id"]): int(row.get("active_skill_level",
                                                             row.get("trained_skill_level", 0)))
                          for row in doc.get("skills", [])}
            except esi_mod.AuthError:
                pass
        selected.append((name, levels))
    if wanted - matched:
        raise RuntimeError("unknown builder(s): " + ", ".join(sorted(wanted - matched)))
    return selected


def _builder_checks(builders, recipe, skill_names):
    checks = []
    for name, levels in builders:
        if recipe.skills is None:
            checks.append({"name": name, "ok": None,
                           "reason": "recipe skills unknown — run: eve-skills update-data"})
            continue
        if levels is None:
            checks.append({"name": name, "ok": None,
                           "reason": "no skills consent — run: eve-skills login --scopes skills"})
            continue
        missing = next(((skill, level) for skill, level in sorted(recipe.skills.items())
                        if levels.get(skill, 0) < level), None)
        if missing is None:
            checks.append({"name": name, "ok": True, "reason": None})
        else:
            skill, level = missing
            checks.append({"name": name, "ok": False,
                           "reason": f"{skill_names.get(skill, f'type {skill}')} {level}"})
    return checks


def cmd_can_build(args):
    """Count installable blueprint jobs from stock in each blueprint's own input location."""
    if args.division is not None and not args.corp:
        raise RuntimeError("--division is only meaningful with can-build --corp")
    bp_scope = ("esi-corporations.read_blueprints.v1" if args.corp
                else "esi-characters.read_blueprints.v1")
    asset_scope = ("esi-assets.read_corporation_assets.v1" if args.corp
                   else "esi-assets.read_assets.v1")
    client, chars, hints = exports.targets(
        args, [("blueprints", bp_scope), ("assets", asset_scope)]
    )
    try:
        recipes = industry.recipes_by_blueprint(alphadata.blueprint_materials())
    except FileNotFoundError:
        raise RuntimeError("no local blueprint data - run: eve-skills update-data") from None
    builders = _can_build_builders(client, args.builders)
    skill_ids = {skill for recipe in recipes.values() for skill in (recipe.skills or {})}
    skill_names = esi_mod.resolve_names(client, skill_ids) if skill_ids else {}
    seen_corps = set()
    documents = []
    query = (args.type or "").strip().casefold()
    for tok, public in chars:
        cid = int(tok["character_id"])
        cname = public.get("name") or tok.get("character_name") or str(cid)
        corp_id = exports.corp_of(public) if args.corp else None
        if args.corp and corp_id is None:
            hints.append(f"warning: {cname}: no corporation id on the public record")
            continue
        if corp_id is not None and corp_id in seen_corps:
            continue
        bp_path = _blueprint_path(cid, corp_id)
        asset_path = (f"/corporations/{corp_id}/assets" if corp_id is not None
                      else f"/characters/{cid}/assets")
        blueprints, bp_meta = client.get_all_meta(bp_path, token=tok["access_token"])
        assets, asset_meta = client.get_all_meta(asset_path, token=tok["access_token"])
        division_names, division_note = ({}, None)
        if corp_id is not None:
            division_names, division_note = divisions.fetch(client, tok, corp_id)
            seen_corps.add(corp_id)
        jobs, jobs_meta = [], None
        required_jobs_scope = ("esi-industry.read_corporation_jobs.v1" if corp_id is not None
                               else "esi-industry.read_character_jobs.v1")
        if required_jobs_scope in set(tok.get("scopes") or []):
            path = _jobs_path(cid, corp_id)
            if corp_id is not None:
                jobs, jobs_meta = client.get_all_meta(
                    f"{path}?include_completed=true", token=tok["access_token"])
            else:
                jobs, jobs_meta = client.get_meta(path, token=tok["access_token"])
        else:
            hints.append(f"{cname}: busy blueprints could not be excluded; "
                         "run: eve-skills login --scopes jobs")
        finished = {"cancelled", "delivered", "reverted"}
        busy = {int(job["blueprint_id"]) for job in jobs
                if job.get("blueprint_id") is not None and job.get("status") not in finished}
        blueprints = [bp for bp in blueprints if int(bp.get("item_id", 0)) not in busy]
        wanted_division = divisions.resolve(args.division, division_names)
        if args.division is not None:
            blueprints = [bp for bp in blueprints
                          if divisions.number(bp.get("location_flag")) == wanted_division]
        type_ids = ({int(bp["type_id"]) for bp in blueprints} |
                    {int(asset["type_id"]) for asset in assets})
        names = esi_mod.resolve_names(client, type_ids | {recipe.product_id for recipe in recipes.values()})
        groups = {}
        for bp in blueprints:
            recipe = recipes.get(int(bp["type_id"]))
            if recipe is None:
                continue
            bp_name = names.get(int(bp["type_id"])) or f"type {bp['type_id']}"
            product_name = names.get(recipe.product_id) or f"type {recipe.product_id}"
            if query and query not in bp_name.casefold() and query not in product_name.casefold():
                continue
            key = (int(bp["type_id"]), int(bp["location_id"]), bp.get("location_flag") or "-",
                   int(bp.get("runs", -1)), int(bp.get("material_efficiency", 0)))
            if key not in groups:
                groups[key] = {"blueprint": bp, "recipe": recipe, "count": 0,
                               "blueprint_name": bp_name, "product": product_name}
            groups[key]["count"] += _row_count(bp)
        location_rows = list(assets) + [
            dict(bp, location_type=("station" if int(bp.get("location_id", 0)) <=
                                      esi_mod.INT32_MAX else "other"))
            for bp in blueprints
        ]
        places = universe.resolve_locations(
            client, location_rows, token=tok["access_token"],
            corporation_id=corp_id, character_id=None if corp_id is not None else cid,
        )
        rows = []
        for group in groups.values():
            bp, recipe = group["blueprint"], group["recipe"]
            runs = int(bp.get("runs", -1))
            runs = runs if runs > 0 else recipe.max_runs
            label = (divisions.label(bp.get("location_flag"), division_names)
                     if corp_id is not None else
                     (places.get(int(bp["location_id"])).name
                      if places.get(int(bp["location_id"])) else f"location {bp['location_id']}"))
            local = {}
            elsewhere = {}
            for asset in assets:
                type_id = int(asset["type_id"])
                qty = int(asset.get("quantity", 0))
                same = (int(asset.get("location_id", 0)) == int(bp["location_id"]) and
                        (corp_id is None or divisions.number(asset.get("location_flag")) ==
                         divisions.number(bp.get("location_flag"))))
                if same:
                    local[type_id] = local.get(type_id, 0) + qty
                else:
                    other = (divisions.label(asset.get("location_flag"), division_names)
                             if corp_id is not None else
                             (places.get(int(asset["location_id"])).name
                              if places.get(int(asset["location_id"])) else
                              f"location {asset['location_id']}"))
                    elsewhere[(type_id, other)] = elsewhere.get((type_id, other), 0) + qty
            requirements = {material: industry.required_quantity(base, runs, int(bp.get(
                "material_efficiency", 0))) for material, base in recipe.materials.items()} if runs > 0 else {}
            ratios = [(local.get(material, 0) // per_job, material, per_job)
                      for material, per_job in requirements.items()]
            possible, binding, per_job = min(ratios) if ratios else (0, 0, 0)
            installable = min(possible, group["count"])
            notes = []
            if binding:
                alternatives = [(qty, where) for (material, where), qty in elsewhere.items()
                                if material == binding and qty > 0]
                if alternatives and local.get(binding, 0) < per_job:
                    qty, where = max(alternatives)
                    notes.append(
                        f"{names.get(binding, f'type {binding}')}: {local.get(binding, 0):,} in "
                        f"{label}, {qty:,} in {where} — the job's Input Material Location must "
                        "point at the hangar holding it"
                    )
            checks = _builder_checks(builders, recipe, skill_names)
            rows.append({
                "blueprint_type_id": int(bp["type_id"]),
                "blueprint": group["blueprint_name"],
                "product_type_id": recipe.product_id,
                "product": group["product"],
                "location_id": int(bp["location_id"]),
                "stock_scope": label,
                "blueprints": group["count"],
                "runs_per_job": runs if runs > 0 else None,
                "me": int(bp.get("material_efficiency", 0)),
                "jobs": installable,
                "limiting_type_id": binding or None,
                "limiting_material": names.get(binding) if binding else None,
                "have": local.get(binding, 0) if binding else None,
                "per_job": per_job or None,
                "builders": checks,
                "notes": notes,
            })
        warnings = []
        if corp_id is not None:
            for subject, meta in (("asset", asset_meta), ("blueprint", bp_meta)):
                count = freshness.delivered_after(jobs, meta)
                warning = freshness.delivery_warning(count, subject)
                if warning:
                    warnings.append(warning)
        documents.append({
            "owner": (f"Corporation {corp_id} (read by {cname})" if corp_id else cname),
            "corporation_id": corp_id,
            "cache": ({
                "assets": freshness.document(asset_meta),
                "blueprints": freshness.document(bp_meta),
                "jobs": freshness.document(jobs_meta) if jobs_meta is not None else None,
            } if corp_id is not None else None),
            "cache_lines": ({
                "assets": freshness.line("corp assets", asset_meta),
                "blueprints": freshness.line("corp blueprints", bp_meta),
                "jobs": freshness.line("corp jobs", jobs_meta) if jobs_meta is not None else None,
            } if corp_id is not None else None),
            "division_note": division_note,
            "warnings": warnings,
            "rows": sorted(rows, key=lambda row: row["product"].casefold()),
        })
    if args.json:
        print(json.dumps({"hints": hints, "documents": documents}, indent=2))
        return
    columns = ["owner", "blueprint_type_id", "blueprint", "product_type_id", "product",
               "stock_scope", "blueprints", "runs_per_job", "me", "jobs",
               "limiting_type_id", "limiting_material", "have", "per_job", "builders", "notes"]
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for document in documents:
            for row in document["rows"]:
                flat = dict(row, owner=document["owner"],
                            builders="; ".join(
                                f"{check['name']}:{'yes' if check['ok'] else 'no' if check['ok'] is False else 'unknown'}"
                                + (f" ({check['reason']})" if check["reason"] else "")
                                for check in row["builders"]),
                            notes="; ".join(row["notes"]))
                writer.writerow({key: flat.get(key, "") for key in columns})
            if document["cache_lines"]:
                for line in document["cache_lines"].values():
                    if line:
                        print(line, file=sys.stderr)
        return
    blocks = list(hints)
    for document in documents:
        lines = [document["owner"],
                 "stock is counted in each blueprint's own division and location"]
        if document["cache_lines"]:
            lines.extend(line for line in document["cache_lines"].values() if line)
        table_rows = []
        for row in document["rows"]:
            builders_cell = ", ".join(
                f"{'✓' if check['ok'] else '✗' if check['ok'] is False else '?'} {check['name']}"
                + (f": {check['reason']}" if check["reason"] else "")
                for check in row["builders"])
            table_rows.append([row["product"], row["stock_scope"], str(row["blueprints"]),
                               str(row["runs_per_job"] or "?"), str(row["me"]), str(row["jobs"]),
                               (f"{row['limiting_material']} {row['have']}/{row['per_job']}"
                                if row["limiting_material"] else "-"), builders_cell or "-"])
            lines.extend(row["notes"])
        lines.append(render.table(
            ["product", "stock scope", "BPs", "runs/job", "ME", "jobs", "limiting", "builders"],
            table_rows) if table_rows else "(no buildable idle blueprints)")
        lines += document["warnings"]
        if document["division_note"]:
            lines.append(document["division_note"])
        blocks.append("\n".join(lines))
    print("\n\n".join(blocks))
