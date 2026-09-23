"""A single, read-only industry snapshot assembled from independent ESI documents."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from . import alphadata, divisions, esi as esi_mod, exports, freshness, industry, market, orders, render, sso, universe


def _read(client, candidates, scope, path, *, paginated=True):
    """Try each consenting colleague until one can read this particular document."""
    refused = []
    for tok, _public in candidates:
        if scope not in set(tok.get("scopes") or []):
            continue
        try:
            fetch = client.get_all_meta if paginated else client.get_meta
            rows, meta = fetch(path, token=tok["access_token"])
            return rows, meta, tok, None
        except esi_mod.AuthError:
            refused.append(int(tok["character_id"]))
    feature = next((name for name, scopes in sso.OPTIONAL_SCOPES.items()
                    if scope in scopes and name != "all"), scope)
    issue = {"code": "esi_refused" if refused else "missing_consent",
             "feature": feature, "scope": scope, "refused_character_ids": refused}
    return [], None, None, issue


def _extras(client, path):
    if not path:
        return set()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as err:
        raise RuntimeError(f"cannot read --materials file '{path}': {err}") from err
    return {market.resolve_type(client, name.strip())[0] for name in lines if name.strip()}


def _cache_line(label, stamp):
    if not stamp["last_modified"]:
        return f"{label} cache time unavailable"
    modified = render.parse_ts(stamp["last_modified"]).strftime("%H:%M")
    text = f"{label} as of {modified} UTC"
    if stamp["expires"]:
        text += f", next refresh {render.parse_ts(stamp['expires']).strftime('%H:%M')}"
    return text

def _issue_text(issue):
    name = issue.get("owner_name") or str(issue.get("owner_id", "industry"))
    section = issue.get("section", "data").replace("_", " ")
    fallback = "; showing numbered divisions" if issue.get("fallback") == "numbered" else ""
    if issue["code"] == "missing_consent":
        return (f"{name} {section}: no {issue['feature']} consent; "
                f"run: eve-skills login --scopes {issue['feature']}{fallback}")
    if issue["code"] == "esi_refused":
        return (f"{name} {section}: ESI refused access for "
                f"{issue['refused_character_ids']}{fallback}")
    if issue["code"] == "missing_recipes":
        return "blueprint recipes unavailable - run: eve-skills update-data"
    if issue["code"] == "missing_corporation":
        return f"{name}: no corporation id on the public character record"
    if issue["code"] == "requires_jobs":
        return f"{name}: idle blueprints and can-build counts unavailable without jobs"
    if issue["code"] == "requires_assets":
        return f"{name}: can-build counts unavailable without assets"
    return f"{name} {section}: unavailable"


def _slots(client, candidates, corp_jobs, corp_available, corp, now, hints, personal_section=None):
    result = []
    for tok, public in candidates:
        cid = int(tok["character_id"])
        name = public.get("name") or tok.get("character_name") or str(cid)
        if personal_section is not None:
            personal, personal_meta, _reader = personal_section
        else:
            personal, personal_meta, _reader, problem = _read(
                client, [(tok, public)], "esi-industry.read_character_jobs.v1",
                f"/characters/{cid}/industry/jobs", paginated=False)
            if problem:
                hints.append(dict(problem, owner_id=cid, owner_name=name, section="personal_jobs"))
        skills = None
        skills_meta = None
        if "esi-skills.read_skills.v1" in set(tok.get("scopes") or []):
            try:
                doc, skills_meta = client.get_meta(f"/characters/{cid}/skills", token=tok["access_token"])
                skills = {int(row["skill_id"]): int(row.get("active_skill_level",
                                                          row.get("trained_skill_level", 0)))
                          for row in doc.get("skills", [])}
            except esi_mod.AuthError:
                hints.append({"owner_id": cid, "owner_name": name, "section": "skills",
                              "code": "esi_refused", "feature": "skills",
                              "scope": "esi-skills.read_skills.v1", "refused_character_ids": [cid]})
        else:
            hints.append({"owner_id": cid, "owner_name": name, "section": "skills",
                          "code": "missing_consent", "feature": "skills",
                          "scope": "esi-skills.read_skills.v1", "refused_character_ids": []})
        jobs = list(personal)
        if corp and corp_available:
            jobs += [job for job in corp_jobs if exports._job_ident(job, "installer_id") == cid]
        complete = personal_meta is not None and (not corp or corp_available)
        unique = {("id", ident) if (ident := exports._job_ident(job, "job_id")) is not None
                  else ("row", id(job)): job for job in jobs}
        counts = {kind: {"used": 0, "ready": 0, "next_end": None} for kind in exports.SLOT_SKILLS}
        for job in unique.values():
            kind = exports.SLOT_ACTIVITY.get(exports._job_ident(job, "activity_id"))
            if not kind:
                continue
            end = render.parse_opt(job.get("end_date"))
            status = exports._job_status(job, end, now)
            if status in {"delivered", "cancelled", "reverted"}:
                continue
            cell = counts[kind]
            cell["used"] += 1
            if status == "ready":
                cell["ready"] += 1
            elif status == "active" and end is not None:
                if cell["next_end"] is None or end < cell["next_end"]:
                    cell["next_end"] = end
        next_end = min((cell["next_end"] for cell in counts.values() if cell["next_end"]),
                       default=None) if complete else None
        for kind, (basic, advanced) in exports.SLOT_SKILLS.items():
            cell = counts[kind]
            maximum = None if skills is None else 1 + skills.get(basic, 0) + skills.get(advanced, 0)
            cell["max"] = maximum
            cell["free"] = max(maximum - cell["used"], 0) if complete and maximum is not None else None
            cell["next_end"] = (cell["next_end"].isoformat().replace("+00:00", "Z")
                                if complete and cell["next_end"] else None)
            if not complete:
                cell["used"] = cell["ready"] = None
        result.append({"character_id": cid, "character_name": name, "complete": complete,
                       "next_end": next_end.isoformat().replace("+00:00", "Z") if next_end else None,
                       "documents": {"personal_jobs": (freshness.document(personal_meta)
                                                        if personal_meta and personal_section is None else None),
                                     "skills": freshness.document(skills_meta) if skills_meta else None},
                       "slots": counts})
    return result


def _job_groups(client, jobs, now):
    rows, ids = exports._job_rows(jobs, now)
    exports._resolve_job_rows(client, rows, ids)
    grouped = defaultdict(list)
    for row in rows:
        if row["status"] in {"ready", "active"}:
            key = (row["status"], row["installer_id"], row["activity_id"], row["product_id"], row["runs"])
            grouped[key].append(row)
    result = {"ready": [], "running": []}
    for (status, installer, activity, product, runs), members in grouped.items():
        ends = sorted(row["end"] for row in members if row["end"] is not None)
        first = members[0]
        result["ready" if status == "ready" else "running"].append({
            "installer_id": installer, "installer_name": first["installer"],
            "activity_id": activity, "activity": first["activity"],
            "product_type_id": product, "product": first["product"], "runs": runs,
            "count": len(members), "end_start": ends[0].isoformat().replace("+00:00", "Z") if ends else None,
            "end_end": ends[-1].isoformat().replace("+00:00", "Z") if ends else None,
            "end_range": exports._end_range(members, now),
        })
    for group in result.values():
        group.sort(key=lambda row: (row["installer_id"] or 0, row["product_type_id"] or 0,
                                    row["activity_id"] or 0, row["runs"], row["end_start"] or ""))
    return result


def _holdings(client, tok, corp_id, blueprints, assets, jobs, jobs_ok, asset_ok, recipes, extras, division_names):
    busy = {int(job["blueprint_id"]) for job in jobs
            if job.get("blueprint_id") is not None and job.get("status") not in
            {"cancelled", "delivered", "reverted"}}
    idle = [bp for bp in blueprints if int(bp["item_id"]) not in busy] if jobs_ok else []
    materials = set(extras)
    for bp in idle:
        recipe = recipes.get(int(bp["type_id"]))
        if recipe:
            materials.update(recipe.materials)
    cid = int(tok["character_id"]) if tok else None
    relevant_assets = [a for a in assets if
                       (int(a["type_id"]) in materials or
                        (corp_id and a.get("location_flag") == "CorpDeliveries")) and
                       a.get("location_type") in {"station", "other"}]
    places = universe.resolve_locations(
        client, relevant_assets + [dict(bp, location_type=("station" if int(bp["location_id"]) <=
                                                                esi_mod.INT32_MAX else "other"))
                                   for bp in blueprints],
        token=tok["access_token"] if tok else None, corporation_id=corp_id,
        character_id=None if corp_id else cid) if tok else {}
    ids = (materials | {int(bp["type_id"]) for bp in idle} |
           {int(a["type_id"]) for a in assets if corp_id and a.get("location_flag") == "CorpDeliveries"})
    ids.update(recipe.product_id for bp in idle if (recipe := recipes.get(int(bp["type_id"]))))
    names = esi_mod.resolve_names(client, ids) if ids else {}
    stock = defaultdict(int)
    available = defaultdict(int)
    for asset in assets:
        flag = asset.get("location_flag") or "-"
        if corp_id and divisions.number(flag) is None:
            continue
        # Use the same local-stock key as can-build: station plus hangar division for
        # corporations, station alone for a personal blueprint.
        loc, type_id = int(asset["location_id"]), int(asset["type_id"])
        qty = int(asset.get("quantity", 0))
        available[(loc, divisions.number(flag) if corp_id else None, type_id)] += qty
        if asset.get("location_type") in {"station", "other"}:
            stock[(loc, flag, type_id)] += qty
    grouped = {}
    for bp in idle:
        type_id, loc = int(bp["type_id"]), int(bp["location_id"])
        flag = bp.get("location_flag") or "-"
        kind = "BPC" if int(bp.get("quantity", 0)) == -2 else "BPO"
        key = (type_id, loc, flag, kind, int(bp.get("runs", -1)),
               int(bp.get("material_efficiency", 0)), int(bp.get("time_efficiency", 0)))
        if key not in grouped:
            grouped[key] = 0
        grouped[key] += int(bp["quantity"]) if int(bp["quantity"]) > 0 else 1
    bp_rows = []
    for (type_id, loc, flag, kind, runs, me, te), count in grouped.items():
        recipe = recipes.get(type_id)
        job_runs = runs if runs > 0 else recipe.max_runs if recipe else None
        requirements = ({mat: industry.required_quantity(qty, job_runs, me)
                         for mat, qty in recipe.materials.items()} if recipe and job_runs else {})
        division = divisions.number(flag) if corp_id else None
        counts = [(available.get((loc, division, mat), 0) // per, mat, per)
                  for mat, per in requirements.items()]
        possible, limiting, per_job = min(counts) if counts else (
            (0, None, None) if recipe else (None, None, None))
        bp_rows.append({
            "blueprint_type_id": type_id, "blueprint": names.get(type_id, f"type {type_id}"),
            "product_type_id": recipe.product_id if recipe else None,
            "product": names.get(recipe.product_id, f"type {recipe.product_id}") if recipe else None,
            "location_id": loc, "location": places[loc].name if loc in places else f"location {loc}",
            "division_number": divisions.number(flag) if corp_id else None,
            "division": divisions.label(flag, division_names) if corp_id else flag,
            "kind": kind, "runs_per_job": job_runs, "me": me, "te": te,
            "count": count, "can_build_jobs": min(possible, count) if possible is not None and asset_ok else None,
            "limiting_type_id": limiting if asset_ok else None,
            "limiting_material": names.get(limiting, f"type {limiting}") if limiting and asset_ok else None,
            "have": available.get((loc, division, limiting), 0) if limiting and asset_ok else None,
            "per_job": per_job if asset_ok else None,
        })
    bp_rows.sort(key=lambda r: (r["blueprint"].casefold(), r["location_id"], r["division"],
                                r["kind"], r["runs_per_job"] or 0, r["me"], r["te"]))
    # An absent required type is zero stock, not an absent row. Keep zero rows tied
    # to an actual blueprint location/division rather than inventing a global location.
    if asset_ok:
        for bp in idle:
            loc, flag = int(bp["location_id"]), bp.get("location_flag") or "-"
            recipe = recipes.get(int(bp["type_id"]))
            for type_id in (materials if recipe is None else set(recipe.materials) | extras):
                stock.setdefault((loc, flag, type_id), 0)
    stock_rows = [{"type_id": type_id, "type_name": names.get(type_id, f"type {type_id}"),
                   "quantity": qty, "location_id": loc,
                   "location": places[loc].name if loc in places else f"location {loc}",
                   "division_number": divisions.number(flag) if corp_id else None,
                   "division": divisions.label(flag, division_names) if corp_id else flag}
                  for (loc, flag, type_id), qty in stock.items() if type_id in materials]
    stock_rows.sort(key=lambda r: (r["location_id"], r["division_number"] or 0,
                                   r["division"], r["type_name"].casefold(), r["type_id"]))
    production_stations = {int(bp["location_id"]) for bp in blueprints}
    deliveries = [{"type_id": int(a["type_id"]),
                   "type_name": names.get(int(a["type_id"]), f"type {a['type_id']}"),
                   "quantity": int(a.get("quantity", 0)), "location_id": int(a["location_id"]),
                   "location": places[int(a["location_id"])].name if int(a["location_id"]) in places
                   else f"location {a['location_id']}",
                   "other_station": True}
                  for a in assets if corp_id and production_stations and
                  a.get("location_flag") == "CorpDeliveries"
                  and int(a["location_id"]) not in production_stations]
    deliveries.sort(key=lambda r: (r["location_id"], r["type_id"]))
    return bp_rows, stock_rows, deliveries


def _table_section(lines, title, headers, rows, limit):
    if not rows:
        lines.append(f"  {title}: (none or unavailable)")
        return
    lines.append(f"  {title}:")
    lines.append(render.table(headers, rows[:limit]))
    if len(rows) > limit:
        lines.append(f"    {len(rows) - limit} more; use --json for the full snapshot")


def cmd_status(args):
    """One diffable snapshot; optional consent failures only remove their own section."""
    if args.industry_action != "status":
        raise RuntimeError("unknown industry action")
    records = sso.list_characters()
    if not records:
        raise RuntimeError("not logged in - run: eve-skills login")
    wanted = sso.resolve_character(args.char) if args.char else None
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    extras = _extras(client, args.materials)
    try:
        recipes = industry.recipes_by_blueprint(alphadata.blueprint_materials())
    except FileNotFoundError:
        recipes = {}
    owners = {}
    unaffiliated = []
    for rec in records:
        cid = int(rec["character_id"])
        if wanted is not None and cid != wanted:
            continue
        tok = sso.get_access_token(cid)
        public = client.get(f"/characters/{cid}")
        corp_id = exports.corp_of(public) if args.corp else None
        if args.corp and corp_id is None:
            unaffiliated.append({"owner_id": cid,
                                  "owner_name": public.get("name") or tok.get("character_name"),
                                  "section": "corporation", "code": "missing_corporation"})
            continue
        owners.setdefault(("corp", corp_id) if args.corp else ("char", cid), []).append((tok, public))
    snapshot = {"owner_kind": "corporation" if args.corp else "character",
                "hints": unaffiliated, "warnings": [], "owners": []}
    if not recipes:
        snapshot["hints"].append({"section": "blueprints", "code": "missing_recipes"})
    now = client.now()
    for (kind, ident), candidates in owners.items():
        corp_id = ident if args.corp else None
        cid = int(candidates[0][0]["character_id"])
        base = f"/corporations/{corp_id}" if corp_id else f"/characters/{cid}"
        cname = candidates[0][1].get("name") or candidates[0][0].get("character_name") or str(cid)
        owner_name = ((esi_mod.resolve_names(client, {corp_id}).get(corp_id) or f"corporation {corp_id}")
                      if corp_id else cname)
        sections = {}
        for title, scope, suffix, paginated in (
            ("jobs", "esi-industry.read_corporation_jobs.v1" if corp_id else
             "esi-industry.read_character_jobs.v1", "/industry/jobs" +
             ("?include_completed=true" if corp_id else ""), bool(corp_id)),
            ("blueprints", "esi-corporations.read_blueprints.v1" if corp_id else
             "esi-characters.read_blueprints.v1", "/blueprints", True),
            ("assets", "esi-assets.read_corporation_assets.v1" if corp_id else
             "esi-assets.read_assets.v1", "/assets", True),
            ("orders", orders.CORPORATION_SCOPE if corp_id else orders.CHARACTER_SCOPE,
             "/orders", bool(corp_id)),
        ):
            rows, meta, reader, problem = _read(client, candidates, scope, base + suffix,
                                                paginated=paginated)
            sections[title] = (rows, meta, reader)
            if problem:
                snapshot["hints"].append(dict(problem, owner_id=ident, owner_name=owner_name,
                                              section=title))
        jobs, jobs_meta, jobs_reader = sections["jobs"]
        blueprints, bp_meta, bp_reader = sections["blueprints"]
        assets, asset_meta, asset_reader = sections["assets"]
        order_rows, order_meta, order_reader = sections["orders"]
        division_names = {}
        division_meta = None
        if corp_id:
            division_doc, division_meta, _reader, problem = _read(
                client, candidates, divisions.SCOPE, base + "/divisions", paginated=False)
            if problem:
                snapshot["hints"].append(dict(problem, owner_id=ident, owner_name=owner_name,
                                              section="divisions", fallback="numbered"))
            else:
                division_names = {int(row["division"]): row["name"] for row in division_doc.get("hangar", [])
                                  if row.get("name") and 1 <= int(row["division"]) <= 7}
        characters = _slots(client, candidates, jobs, jobs_meta is not None, bool(corp_id), now,
                            snapshot["hints"], None if corp_id else sections["jobs"])
        if bp_meta and not jobs_meta:
            snapshot["hints"].append({"owner_id": ident, "owner_name": owner_name,
                                      "section": "blueprints", "code": "requires_jobs"})
        if bp_meta and not asset_meta:
            snapshot["hints"].append({"owner_id": ident, "owner_name": owner_name,
                                      "section": "blueprints", "code": "requires_assets"})
        bp_rows, stock_rows, deliveries = _holdings(
            client, bp_reader or asset_reader, corp_id, blueprints, assets, jobs,
            jobs_meta is not None, asset_meta is not None, recipes, extras, division_names)
        order_objects = [orders.normalise(row, orders.owner_key(kind, ident), owner_name,
                                          corporation=bool(corp_id)) for row in order_rows]
        order_ids = {i for order in order_objects for i in (order.type_id, order.region_id,
                                                              order.location_id, order.issued_by) if i}
        order_names = esi_mod.resolve_names(client, order_ids) if order_ids else {}
        order_list = [{"order_id": order.order_id, "type_id": order.type_id,
                       "type_name": order_names.get(order.type_id, f"type {order.type_id}"),
                       "is_buy": order.is_buy, "price": order.price,
                       "volume_total": order.volume_total, "volume_remain": order.volume_remain,
                       "filled": order.filled,
                       "filled_pct": (order.filled / order.volume_total * 100 if order.volume_total else None),
                       "location_id": order.location_id,
                       "location_name": order_names.get(order.location_id, f"location {order.location_id}"),
                       "region_id": order.region_id,
                       "region_name": order_names.get(order.region_id, f"region {order.region_id}"),
                       "issued_by": order.issued_by,
                       "issued_by_name": order_names.get(order.issued_by) if order.issued_by else None}
                      for order in order_objects]
        order_list.sort(key=lambda row: row["order_id"])
        warnings = []
        for subject, meta in (("asset", asset_meta), ("blueprint", bp_meta)):
            if jobs_meta is not None and meta is not None:
                count = freshness.delivered_after(jobs, meta)
                if count:
                    issue = {"owner_id": ident, "subject": subject, "delivered_jobs": count}
                    warnings.append(issue)
                    snapshot["warnings"].append(issue)
        snapshot["owners"].append({
            "owner_id": ident, "owner_name": owner_name,
            "read_by_character_id": int(jobs_reader["character_id"]) if jobs_reader and corp_id else None,
            "characters": characters, "availability": {
                title: meta is not None for title, (_rows, meta, _tok) in sections.items()},
            "documents": {title: freshness.document(meta) if meta is not None else None
                          for title, (_rows, meta, _tok) in sections.items()} |
                         ({"divisions": freshness.document(division_meta) if division_meta else None}
                          if corp_id else {}),
            "jobs": _job_groups(client, jobs, now), "blueprints": bp_rows,
            "materials": stock_rows, "orders": order_list, "deliveries": deliveries,
            "warnings": warnings,
        })
    if args.json:
        for owner in snapshot["owners"]:
            for group in owner["jobs"].values():
                for row in group:
                    row.pop("end_range", None)  # Relative text belongs only in the human table.
        print(json.dumps(snapshot, indent=2))
        return
    lines = [_issue_text(issue) for issue in snapshot["hints"]]
    for owner in snapshot["owners"]:
        lines.append(f"{owner['owner_name']} (id {owner['owner_id']})")
        for title, stamp in owner["documents"].items():
            if stamp is not None:
                lines.append(_cache_line(
                    f"{'corp' if snapshot['owner_kind'] == 'corporation' else 'character'} {title}",
                    stamp))
        for character in owner["characters"]:
            for title, stamp in character["documents"].items():
                if stamp:
                    lines.append(_cache_line(f"{character['character_name']} {title}", stamp))
        if not (owner["jobs"]["ready"] or owner["jobs"]["running"] or
                owner["blueprints"] or owner["materials"] or owner["orders"] or owner["deliveries"]):
            for c in owner["characters"]:
                slots = ", ".join(
                    f"{kind} {v['used'] if v['used'] is not None else '?'}/"
                    f"{v['max'] if v['max'] is not None else '?'}/"
                    f"{v['free'] if v['free'] is not None else '?'}"
                    for kind, v in c["slots"].items())
                lines.append(f"  {c['character_name']} slots used/max/free: {slots}")
            lines.append("  no readable jobs, blueprints, material stock, orders or deliveries")
            lines.extend(f"  warning: {freshness.delivery_warning(issue['delivered_jobs'], issue['subject'])}"
                         for issue in owner["warnings"])
            continue
        _table_section(lines, "slots",
                       ["character", "manufacturing used/max/free", "science used/max/free",
                        "reaction used/max/free", "next running end"],
                       [[c["character_name"]] +
                        [f"{v['used'] if v['used'] is not None else '?'}/"
                         f"{v['max'] if v['max'] is not None else '?'}/"
                         f"{v['free'] if v['free'] is not None else '?'} "
                         f"({v['ready'] if v['ready'] is not None else '?'} ready)"
                         for v in c["slots"].values()] + [c["next_end"] or "-"]
                        for c in owner["characters"]], 12)
        for group in ("ready", "running"):
            _table_section(lines, f"{group} jobs", ["count", "activity", "product", "runs",
                                                    "installer", "end range"],
                           [[str(j["count"]), j["activity"],
                             f"{j['product']} ({j['product_type_id']})", j["runs"],
                             j["installer_name"], j["end_range"]]
                            for j in owner["jobs"][group]], 5)
        _table_section(lines, "idle blueprints",
                       ["count", "blueprint", "kind", "runs", "location / division", "jobs"],
                       [[str(bp["count"]), f"{bp['blueprint']} ({bp['blueprint_type_id']})",
                         bp["kind"], str(bp["runs_per_job"] or "?"),
                         f"{bp['location']} / {bp['division']}",
                         str(bp["can_build_jobs"]) if bp["can_build_jobs"] is not None else "?"]
                        for bp in owner["blueprints"]], 8)
        _table_section(lines, "material stock", ["material", "quantity / location / division"],
                       [[f"{r['type_name']} ({r['type_id']})",
                         f"{r['quantity']:,} at {r['location']} / {r['division']}"]
                        for r in owner["materials"]], 10)
        _table_section(lines, "open orders", ["type", "side", "remaining/total",
                                             "filled", "location"],
                       [[f"{r['type_name']} ({r['type_id']})", "buy" if r["is_buy"] else "sell",
                         f"{r['volume_remain']:,}/{r['volume_total']:,}",
                         f"{r['filled_pct']:.0f}% filled" if r["filled_pct"] is not None else "?",
                         r["location_name"]] for r in owner["orders"]], 8)
        _table_section(lines, "deliveries at other stations", ["type", "quantity / location"],
                       [[f"{r['type_name']} ({r['type_id']})",
                         f"{r['quantity']:,} at {r['location']}"] for r in owner["deliveries"]], 8)
        lines.extend(f"  warning: {freshness.delivery_warning(issue['delivered_jobs'], issue['subject'])}"
                     for issue in owner["warnings"])
    print("\n".join(lines))
