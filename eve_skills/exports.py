"""Extra per-character views beyond skills: standings, industry, assets, location/clones, implants.

Every command walks all stored characters unless --char selects one. Missing consent degrades to
a hint line (exit 0): granting a scope means a browser re-auth per character, so that stays the
user's decision - never triggered implicitly.
"""

from __future__ import annotations

import csv
import io
import sys

from . import esi as esi_mod, render, sso

ACTIVITY = {1: "manufacturing", 2: "time efficiency research", 3: "material efficiency research",
            4: "copying", 5: "invention", 8: "reaction"}


def hint(name: str, feature: str) -> str:
    return (f"{name}: no {feature} consent - run: eve-skills login --scopes {feature}"
            f"  (pick '{name}' in the browser)")


def targets(args, features):
    """(client, [(token_record, public_doc)], hints).

    `features` is [(feature, required_scope)]; a character qualifies when its consent covers
    at least one of the listed scopes. public_doc is fetched only for qualifying characters -
    it carries the corporation id needed by the corp variants.
    """
    records = sso.list_characters()
    if not records:
        raise RuntimeError("not logged in - run: eve-skills login")
    wanted = sso.resolve_character(args.char) if getattr(args, "char", None) else None
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    chars, hints = [], []
    for rec in records:
        if wanted is not None and int(rec["character_id"]) != wanted:
            continue
        tok = sso.get_access_token(int(rec["character_id"]))
        name = tok.get("character_name") or str(tok["character_id"])
        granted = set(tok.get("scopes") or [])
        if not any(scope in granted for _, scope in features):
            msg = hint(name, features[0][0])
            hints.append(msg)
            if getattr(args, "csv", False):
                print(msg, file=sys.stderr)  # CSV stdout stays machine-readable
            continue
        public = client.get(f"/characters/{tok['character_id']}")
        chars.append((tok, public))
    return client, chars, hints


def corp_of(public: dict) -> int | None:
    corp = public.get("corporation_id")
    if not corp and isinstance(public.get("corporation"), dict):
        corp = public["corporation"].get("id")
    return int(corp) if corp else None


def name_or_id(names: dict[int, str], ident) -> str:
    if ident is None:
        return "-"
    return names.get(int(ident), f"id {ident}")


def _standing_entries(doc: list[dict], names: dict[int, str]) -> list[tuple[str, int, str, float]]:
    kind_labels = {"agent": "agent", "npc_corp": "npc corp", "faction": "faction"}
    entries = [
        (kind_labels.get(e.get("from_type"), e.get("from_type") or "unknown"),
         int(e["from_id"]), name_or_id(names, e["from_id"]), float(e.get("standing") or 0))
        for e in doc
    ]
    return sorted(entries, key=lambda entry: entry[2])


def cmd_standings(args):
    client, chars, hints = targets(args, [("standings", "esi-characters.read_standings.v1")])
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n") if args.csv else None
    if writer:
        writer.writerow(["character", "kind", "from_id", "name", "standing"])
    blocks = list(hints)
    for tok, public in chars:
        doc = client.get(f"/characters/{tok['character_id']}/standings", token=tok["access_token"])
        ids = {int(entry["from_id"]) for entry in doc}
        names = esi_mod.resolve_names(client, ids) if ids else {}
        entries = _standing_entries(doc, names)
        cname = public.get("name") or str(tok["character_id"])
        if writer:
            for kind, fid, fname, standing in entries:
                writer.writerow([cname, kind, fid, fname, f"{standing:+.2f}"])
        else:
            table = render.table(["kind", "name", "standing"],
                                 [[k, n, f"{s:+.2f}"] for k, _, n, s in entries]) if entries else "(no standings recorded)"
            blocks.append(f"{cname} (id {tok['character_id']})\n{table}")
    if writer:
        sys.stdout.write(buf.getvalue())
    else:
        print("\n\n".join(blocks))


def _job_rows(jobs, now):
    """Rows with raw ids in columns 2/5; the caller resolves names."""
    ids = {int(j["output_type_id"]) for j in jobs if j.get("output_type_id")}
    ids |= {int(j["installed_in"]) for j in jobs if j.get("installed_in")}
    rows = []
    for j in sorted(jobs, key=lambda j: j.get("finish_date") or "9999"):
        finish = render.parse_opt(j.get("finish_date"))
        if j.get("status") == "active" and finish:
            time_left = f"{render.format_duration(max((finish - now).total_seconds(), 0))} left"
        elif finish:
            time_left = finish.strftime("%b %d %H:%M")
        else:
            time_left = "-"
        runs = (f"{j.get('installed_runs', '?')}/{j.get('runs', '?')}"
                if j.get("runs") or j.get("installed_runs") else "-")
        rows.append([j.get("status", "?"), ACTIVITY.get(j.get("activity"), f"activity {j.get('activity')}"),
                     int(j.get("output_type_id") or 0), runs, time_left, int(j.get("installed_in") or 0)])
    return rows, ids


def cmd_jobs(args):
    scope = "esi-industry.read_corporation_jobs.v1" if args.corp else "esi-industry.read_character_jobs.v1"
    client, chars, hints = targets(args, [("jobs", scope)])
    now = client.now()
    blocks, failures, all_rows = list(hints), [], []
    for tok, public in chars:
        cid = tok["character_id"]
        cname = public.get("name") or str(cid)
        try:
            if args.corp:
                corp_id = corp_of(public)
                if not corp_id:
                    failures.append(f"{cname}: no corporation id on the public record")
                    continue
                path = f"/corporations/{corp_id}/industry/jobs"
                if args.completed:
                    path += "?include_completed=true"
                jobs = client.get_all(path, token=tok["access_token"])
            else:
                path = f"/characters/{cid}/industry/jobs"
                if args.completed:
                    path += "?include_completed=true"
                jobs = client.get(path, token=tok["access_token"])
        except esi_mod.AuthError as err:
            failures.append(f"{cname}: ESI refused ({err}) - corporation endpoints need the matching director/Account-Manager role")
            continue
        rows, ids = _job_rows(jobs, now)
        names = esi_mod.resolve_names(client, ids) if ids else {}
        for r in rows:
            r[2] = name_or_id(names, r[2] or None)
            r[5] = name_or_id(names, r[5] or None)
        all_rows.append((cname, rows))
    for line in failures:
        print(f"warning: {line}", file=sys.stderr)
    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["character", "status", "activity", "product", "runs", "time", "installed_in"])
        for cname, rows in all_rows:
            for r in rows:
                writer.writerow([cname] + r)
    else:
        for cname, rows in all_rows:
            table = render.table(["status", "activity", "product", "runs", "time", "installed in"], rows) if rows else "(no jobs)"
            blocks.append(f"{cname}\n{table}")
        print("\n\n".join(blocks))


def cmd_inventory(args):
    scope = "esi-assets.read_corporation_assets.v1" if args.corp else "esi-assets.read_assets.v1"
    client, chars, hints = targets(args, [("assets", scope)])
    blocks, failures, csv_rows = list(hints), [], []
    for tok, public in chars:
        cid = tok["character_id"]
        cname = public.get("name") or str(cid)
        try:
            if args.corp:
                corp_id = corp_of(public)
                if not corp_id:
                    failures.append(f"{cname}: no corporation id on the public record")
                    continue
                assets = client.get_all(f"/corporations/{corp_id}/assets", token=tok["access_token"])
            else:
                assets = client.get_all(f"/characters/{cid}/assets", token=tok["access_token"])
        except esi_mod.AuthError as err:
            failures.append(f"{cname}: ESI refused ({err}) - corporation assets need the director/Account-Manager role for that corp")
            continue
        ids = {int(a["type_id"]) for a in assets} | {int(a["location_id"]) for a in assets}
        names = esi_mod.resolve_names(client, ids) if ids else {}
        for a in assets:
            a["_type"] = name_or_id(names, a["type_id"])
            a["_loc"] = name_or_id(names, a["location_id"])
        csv_rows.extend((cname, a) for a in assets)
        if args.items:
            rows = [[a["_type"], str(a.get("quantity", 1)), a["_loc"], a.get("flag", "-")] for a in assets]
            table = render.table(["item", "qty", "location", "flag"], rows) if rows else "(inventory empty)"
        else:
            groups: dict[tuple, list] = {}
            for a in assets:
                g = groups.setdefault((a["_loc"], a.get("flag", "-")), [set(), 0, 0])
                g[0].add(a["type_id"])
                g[1] += int(a.get("quantity", 1))
                g[2] += 1 if a.get("is_singleton") else 0
            rows = [[loc, flag, str(len(g[0])), f"{g[1]:,}", str(g[2])] for (loc, flag), g in sorted(groups.items())]
            table = render.table(["location", "flag", "types", "units", "singletons"], rows) if rows else "(inventory empty)"
        blocks.append(f"{cname} ({len(assets):,} asset rows)\n{table}")
    for line in failures:
        print(f"warning: {line}", file=sys.stderr)
    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["character", "item_id", "type_id", "item_name", "quantity", "singleton", "flag", "location_id", "location_name"])
        for cname, a in csv_rows:
            writer.writerow([cname, a["item_id"], a["type_id"], a["_type"], a.get("quantity", 1),
                             int(bool(a.get("is_singleton"))), a.get("flag", ""), a["location_id"], a["_loc"]])
    else:
        print("\n\n".join(blocks))


def cmd_travel(args):
    client, chars, hints = targets(args, [("clones", "esi-clones.read_clones.v1"),
                                          ("location", "esi-location.read_location.v1")])
    blocks, csv_rows = list(hints), []
    for tok, public in chars:
        cid = tok["character_id"]
        cname = public.get("name") or str(cid)
        granted = set(tok.get("scopes") or [])
        lines = [f"{cname} (id {cid})"]
        loc = None
        if "esi-location.read_location.v1" in granted:
            try:
                loc = client.get(f"/characters/{cid}/location", token=tok["access_token"]) or {}
            except esi_mod.AuthError as err:
                lines.append(f"  location: ESI refused ({err})")
        else:
            lines.append("  " + hint(cname, "location"))
        clones_doc = {}
        try:
            clones_doc = client.get(f"/characters/{cid}/clones", token=tok["access_token"]) or {}
        except esi_mod.AuthError as err:
            lines.append(f"  clones: ESI refused ({err})")
        ids = set()
        if loc:
            ids |= {int(v) for v in (loc.get("solar_system_id"), loc.get("station_id"), loc.get("structure_id")) if v}
        home = clones_doc.get("home_location") or {}
        if home:
            ids.add(int(home["location_id"]))
        jump_clones = clones_doc.get("jump_clones") or []
        for jc in jump_clones:
            ids.add(int(jc["location_id"]))
            ids |= {int(i) for i in (jc.get("implants") or [])}
        names = esi_mod.resolve_names(client, ids) if ids else {}
        where = name_or_id(names, loc.get("station_id") or loc.get("structure_id")) if loc else "-"
        system = name_or_id(names, loc.get("solar_system_id")) if loc else "-"
        if loc:
            lines.append(f"  current: {where}" + (f" ({system})" if where not in ("-", system) else ""))
        if home:
            lines.append(f"  home: {name_or_id(names, home['location_id'])}")
        if clones_doc.get("last_clone_jump_date"):
            lines.append(f"  last clone jump: {render.parse_ts(clones_doc['last_clone_jump_date']).strftime('%Y-%m-%d %H:%M')} UTC")
        if jump_clones:
            rows = []
            for jc in sorted(jump_clones, key=lambda j: j.get("name") or ""):
                imp = ", ".join(name_or_id(names, i) for i in (jc.get("implants") or [])) or "-"
                rows.append([jc.get("name") or "(unnamed)", name_or_id(names, jc["location_id"]), imp])
            lines.append(render.table(["jump clone", "location", "implants"], rows))
        if loc:
            csv_rows.append([cname, "current", where if where != "-" else system, "", ""])
        if home:
            csv_rows.append([cname, "home", name_or_id(names, home["location_id"]), "", ""])
        for jc in jump_clones:
            imp = ", ".join(name_or_id(names, i) for i in (jc.get("implants") or []))
            csv_rows.append([cname, "jump clone", name_or_id(names, jc["location_id"]), jc.get("name") or "(unnamed)", imp])
        blocks.append("\n".join(lines))
    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["character", "kind", "location", "clone_name", "implants"])
        writer.writerows(csv_rows)
    else:
        print("\n\n".join(blocks))


def cmd_implants(args):
    client, chars, hints = targets(args, [("clones", "esi-clones.read_implants.v1")])
    blocks, csv_rows = list(hints), []
    for tok, public in chars:
        cid = tok["character_id"]
        cname = public.get("name") or str(cid)
        doc = client.get(f"/characters/{cid}/implants", token=tok["access_token"])
        ids = doc.get("implants") if isinstance(doc, dict) else doc
        ids = [int(i) for i in (ids or [])]
        names = esi_mod.resolve_names(client, set(ids)) if ids else {}
        # one row per implant instance: the same type can be fitted in both head slots
        rows = [[names.get(i, f"id {i}")] for i in sorted(ids, key=lambda i: names.get(i, ""))]
        csv_rows.extend([cname, r[0]] for r in rows)
        table = render.table(["implant"], rows) if rows else "(no implants fitted)"
        blocks.append(f"{cname} (id {cid})\n{table}")
    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["character", "implant"])
        writer.writerows(csv_rows)
    else:
        print("\n\n".join(blocks))
