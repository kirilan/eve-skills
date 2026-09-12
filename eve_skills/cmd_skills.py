"""skills, summary, plan and extract: the training book and what reaching a level costs."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import timedelta
from dataclasses import dataclass


from . import alphadata, classify, esi as esi_mod, planner, render, snapshots, sso, watchstate


def gather(args, client: esi_mod.Esi | None = None) -> dict:
    char_id = sso.resolve_character(args.char) if getattr(args, "char", None) else None
    token = sso.get_access_token(char_id)
    cfg = sso.load_config()
    # One client per command/watch session: the response cache and the ESI error-limit
    # backoff only work when state survives across characters and polls.
    client = client or esi_mod.Esi(esi_mod.default_user_agent(cfg))
    char_id = token["character_id"]

    public = client.get(f"/characters/{char_id}")
    skills_doc = client.get(f"/characters/{char_id}/skills", token=token["access_token"])
    queue = client.get(f"/characters/{char_id}/skillqueue", token=token["access_token"])
    queue.sort(key=lambda q: (q.get("queue_position", 0), q.get("start_date") or "9999"))

    now = client.now()  # ESI server time; immune to a skewed system clock
    # The skills endpoint can lag behind finished training until the character logs in;
    # apply completed queue entries on top (per ESI docs).
    trained_by_id = {int(s["skill_id"]): int(s["trained_skill_level"]) for s in skills_doc.get("skills", [])}
    completed_levels: dict[int, int] = {}
    for item in queue:
        if queue_status(item, now) == "done":
            sid = int(item["skill_id"])
            level = int(item.get("finished_level") or 0)
            if level > trained_by_id.get(sid, 0):
                completed_levels[sid] = max(completed_levels.get(sid, 0), level)

    data = alphadata.load()
    caps, grade_name = classify.alpha_caps(data, public.get("bloodline_id"))
    names = esi_mod.resolve_names(client, {int(s["skill_id"]) for s in skills_doc.get("skills", [])} | {int(q["skill_id"]) for q in queue})
    known_ids = set(names)

    extra_warnings = []
    age = alphadata.data_age_days(data)
    if age is not None and age > alphadata.STALE_DAYS:
        extra_warnings.append(
            f"local alpha caps data is {age:.0f} days old (SDE build {data['grades']['build']}) - run: eve-skills update-data"
        )

    rows = classify.classify_skills(skills_doc.get("skills", []), caps, names, completed_levels, known_ids=known_ids)
    state = classify.clone_state(
        rows, [q for q in queue if queue_status(q, now) != "done"], caps, names,
        now=now, known_ids=known_ids, extra_warnings=extra_warnings,
    )

    try:
        # ESI's clock, not the machine's: `week_line` and the rate calibration measure these rows
        # against `now`, and a stamp from a different clock makes that span wrong by the skew.
        snapshots.record(char_id, int(skills_doc.get("total_sp") or 0), now=now.timestamp())
    except OSError:
        pass  # history is diagnostics; never fail the view over it

    return {
        "now": now,
        "token": token,
        "public": public,
        "skills_doc": skills_doc,
        "queue": queue,
        "rows": rows,
        "state": state,
        "caps": caps,
        "grade_name": grade_name,
        "data_build": data["grades"]["build"],
        "names": names,
    }


FILTERS = {
    # Bucket by what alpha can actually show, independent of the current clone state:
    # a downgraded omega keeps trained levels above the cap.
    "alpha": lambda r: not r.beyond_alpha,
    "omega": lambda r: r.beyond_alpha,
}


def queue_status(item: dict, now) -> str:
    """done | training | queued | blocked (CCP issued no schedule dates - item cannot train)."""
    if not item.get("start_date") or not item.get("finish_date"):
        return "blocked"
    if render.parse_ts(item["finish_date"]) <= now:
        return "done"
    if render.parse_ts(item["start_date"]) <= now:
        return "training"
    return "queued"


def level_sp_cell(item: dict, now, status: str) -> str:
    """`204.3K/210.7K 97%` - SP into the level being trained, out of what the level costs.

    ESI publishes three SP figures per queue item and they answer three different questions:
    `level_start_sp` is where the level began, `level_end_sp` is where it ends, and
    `training_start_sp` is what the character held when `start_date` was stamped. Progress through
    a level is an SP question, so it is answered from those three - never from the fraction of
    `start_date`..`finish_date` that has elapsed. EVE restamps `start_date` on the active item
    every time the queue is rearranged, so that fraction describes the current sitting at the
    keyboard rather than the level: an hour after a reorder a level 95% trained reads as 11%, under
    a column headed `level sp`.

    SP does accrue linearly across the span, which is what makes the interpolation from
    `training_start_sp` to `level_end_sp` exact and makes the restamped stamps harmless here - the
    span and `training_start_sp` are the pair that describe each other. A queued item has trained
    nothing yet and a blocked one never will, so both keep the dash the timing column explains.
    """
    level_start, level_end = item.get("level_start_sp"), item.get("level_end_sp")
    if level_start is None or level_end is None or int(level_end) <= int(level_start):
        # Nothing measurable was published. "done" is still known to be done - it finished - but a
        # figure this column cannot source is a dash, not an invented percentage.
        return "100%" if status == "done" else "-"
    level_start, level_end = int(level_start), int(level_end)
    if status == "done":
        held: float = level_end
    elif status == "training" and item.get("training_start_sp") is not None:
        start = render.parse_opt(item.get("start_date"))
        finish = render.parse_opt(item.get("finish_date"))
        if start is None or finish is None or finish <= start:
            return "-"
        at_start = int(item["training_start_sp"])
        span = (finish - start).total_seconds()
        # Clamped because ESI's stamps and its clock can disagree by a poll: a hair past the finish
        # is 100% of the level, never 101%.
        elapsed = min(max((now - start).total_seconds(), 0.0), span)
        held = at_start + (level_end - at_start) * elapsed / span
    else:
        return "-"
    # Both figures are level-relative, like the percentage beside them. The cumulative pair ESI
    # publishes (SP held in the *skill* over SP the skill ends the level with) would put a ratio
    # next to a percentage it contradicts: a rank-1 skill halfway through L1->L2 holds 832 of 1,414
    # cumulative SP, which reads as 59% of a level that is 50% trained.
    into_level, level_cost = held - level_start, level_end - level_start
    return (f"{render.format_sp(int(round(into_level)))}/{render.format_sp(level_cost)} "
            f"{into_level / level_cost:.0%}")


def render_text(ctx: dict, args) -> str:
    now = ctx["now"]
    public = ctx["public"]
    out: list[str] = []

    bloodline_race = classify.race_name(alphadata.load()["races"]["races"].get(str(public.get("bloodline_id"))))
    state = ctx["state"]
    out.append(f"{public.get('name')} (id {ctx['token']['character_id']})  {bloodline_race}  clone grade: {ctx['grade_name']}")
    out.append(f"Clone state: {state.state}  (confidence: {state.confidence})")
    for line in state.evidence[:3]:
        out.append(f"  evidence: {line}")
    for line in state.warnings:
        out.append(f"  warning: {line}")

    total = ctx["skills_doc"].get("total_sp", 0)
    unalloc = ctx["skills_doc"].get("unallocated_sp") or 0
    out.append(f"Total SP: {render.format_sp(total)}   unallocated: {render.format_sp(unalloc)}")

    queue = ctx["queue"]
    if queue and not args.trained_only:
        out.append("")
        out.append(f"TRAINING QUEUE ({len(queue)})")
        rows = []
        for item in queue:
            sid = int(item["skill_id"])
            status = queue_status(item, now)
            start = render.parse_opt(item.get("start_date"))
            finish = render.parse_opt(item.get("finish_date"))
            progress = level_sp_cell(item, now, status)
            if status == "done":
                label, when = "done", "finished (pending login)"
            elif status == "training":
                label = "TRAINING"
                when = f"{render.format_duration((finish - now).total_seconds())} left"
            elif status == "blocked":
                label, when = "BLOCKED", "no schedule - cannot train"
            else:
                label = "queued"
                when = f"starts in {render.format_duration((start - now).total_seconds())}, {render.format_duration((finish - start).total_seconds())} long"
            cap = ctx["caps"].get(sid)
            access = "alpha" if cap is not None and item.get("finished_level", 0) <= cap else "OMEGA"
            rows.append([label, ctx["names"].get(sid, f"skill {sid}"), f"L{item.get('finished_level', '?')}", progress, when, access])
        out.append(render.table(["status", "skill", "to", "level sp", "timing", "access"], rows))

    # ESI also lists never-trained prerequisite skills; the table is about what you have.
    rows_all = [r for r in ctx["rows"] if r.trained > 0 or r.pending_completion]
    alpha_n = sum(1 for r in rows_all if not r.beyond_alpha)
    # A row the local data could not classify has no cap, so `beyond_alpha` is true for it - but the
    # warning three lines up has already said no omega claim is being made about it. Counting it as
    # omega-restricted would make one screen assert and disclaim the same thing, so it gets its own
    # bucket. The row's own cell already reads "unknown data"; the header now agrees with it.
    unknown_n = sum(1 for r in rows_all if r.unknown_data)
    omega_n = len(rows_all) - alpha_n - unknown_n
    filtered = [r for r in rows_all if FILTERS[args.filter](r)] if args.filter != "all" else rows_all

    if args.sort == "name":
        filtered.sort(key=lambda r: r.name.lower())
    elif args.sort == "level":
        filtered.sort(key=lambda r: (-r.trained, r.name.lower()))
    elif args.sort == "sp":
        filtered.sort(key=lambda r: -r.sp)

    out.append("")
    scope = "" if args.filter == "all" else f" (filter: {args.filter})"
    counts = f"alpha-trainable: {alpha_n}, omega-restricted: {omega_n}"
    if unknown_n:
        counts += f", unclassified: {unknown_n}"
    out.append(f"TRAINED SKILLS ({len(filtered)} of {len(rows_all)}; {counts}){scope}")
    table_rows = []
    for r in filtered:
        level = f"{r.trained} (active {r.active})" if r.restricted else str(r.trained)
        if r.pending_completion:
            level += " *"
        if r.cap is None:
            access = "unknown data" if r.unknown_data else "OMEGA-only"
        else:
            access = f"alpha <= {r.cap}" + (" (exceeded)" if r.trained > r.cap else "")
        table_rows.append([r.name, level, render.format_sp(r.sp), access])
    out.append(render.table(["skill", "level", "sp", "clone access"], table_rows))
    if any(r.pending_completion for r in filtered):
        out.append("* completed training, not yet reflected by ESI (applies on next login)")

    out.append("")
    out.append(f"alpha caps source: SDE build {ctx['data_build']} (eve-skills update-data to refresh)")
    return "\n".join(out)


def render_json(ctx: dict, args) -> str:
    now = ctx["now"]
    public = ctx["public"]
    race_id = alphadata.load()["races"]["races"].get(str(public.get("bloodline_id")))
    doc = {
        "character": {
            "id": ctx["token"]["character_id"],
            "name": public.get("name"),
            "bloodline_id": public.get("bloodline_id"),
            "race_id": race_id,
            "race": classify.race_name(race_id),
            "alpha_grade": ctx["grade_name"],
        },
        "clone_state": {
            "state": ctx["state"].state,
            "confidence": ctx["state"].confidence,
            "evidence": ctx["state"].evidence,
            "warnings": ctx["state"].warnings,
        },
        "totals": {
            "total_sp": ctx["skills_doc"].get("total_sp"),
            "unallocated_sp": ctx["skills_doc"].get("unallocated_sp"),
        },
        "queue": [
            {
                "skill_id": int(q["skill_id"]),
                "name": ctx["names"].get(int(q["skill_id"])),
                "finished_level": q.get("finished_level"),
                "start_date": q.get("start_date"),
                "finish_date": q.get("finish_date"),
                "status": queue_status(q, now),
            }
            for q in ctx["queue"]
        ],
        "skills": [
            {
                "skill_id": r.skill_id,
                "name": r.name,
                "trained_level": r.trained,
                "active_level": r.active,
                "sp": r.sp,
                "alpha_cap": r.cap,
                "access": "omega" if r.beyond_alpha else "alpha",
                "restricted": r.restricted,
                "pending_completion": r.pending_completion,
                "unknown_data": r.unknown_data,
            }
            for r in ctx["rows"]
        ],
        "data_build": ctx["data_build"],
    }
    return json.dumps(doc, indent=2)


@dataclass(frozen=True)
class FetchFailure:
    """One character whose fetch failed this cycle. str() keeps the legacy warning
    wording, so one-shot commands print exactly what they always printed."""
    character_id: int
    name: str
    error: str

    def __str__(self) -> str:
        return f"{self.name}: {self.error}"


def gather_all(args, client: esi_mod.Esi | None = None):
    """(contexts, FetchFailure list) for the selected character, or every stored one."""
    records = sso.list_characters()
    if not records:
        raise RuntimeError("not logged in — run: eve-skills login")
    wanted = sso.resolve_character(args.char) if getattr(args, "char", None) else None
    ctxs, failures = [], []
    for rec in records:
        if wanted is not None and int(rec["character_id"]) != wanted:
            continue
        sub_args = argparse.Namespace(**vars(args))
        sub_args.char = str(wanted if wanted is not None else rec["character_id"])
        try:
            ctxs.append(gather(sub_args, client=client))
        except (RuntimeError, esi_mod.EsiError) as err:
            failures.append(FetchFailure(int(rec["character_id"]),
                                         rec.get("character_name") or str(rec["character_id"]), str(err)))
    return ctxs, failures


def week_line(ctx) -> str:
    """SP gained since the newest snapshot at least 7 days old; honest when history is short."""
    char_id = ctx["token"]["character_id"]
    now_ts = ctx["now"].timestamp()
    cur = int(ctx["skills_doc"].get("total_sp") or 0)
    base = snapshots.latest_before(char_id, now_ts - 7 * 86400)
    if base is None:
        first = min((r["ts"] for r in snapshots.load() if r["char_id"] == char_id), default=now_ts)
        return f"SP last 7d: no baseline yet (local history starts {(now_ts - first) / 86400:.1f} days ago)"
    days = max((now_ts - base["ts"]) / 86400, 1e-9)
    delta = cur - base["total_sp"]
    return f"SP last 7d: {delta:+,} over {days:.1f} days ({render.format_sp(int(delta / days))}/day)"


def render_csv(ctxs) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["character_id", "character_name", "skill_id", "name", "trained_level", "active_level",
                     "sp", "alpha_cap", "access", "restricted", "pending_completion", "unknown_data"])
    for ctx in ctxs:
        cid = ctx["token"]["character_id"]
        cname = ctx["public"].get("name") or ""
        for r in ctx["rows"]:
            if r.trained == 0 and not r.pending_completion:
                continue  # ESI lists never-trained prerequisites; the table view filters them - CSV must agree
            writer.writerow([cid, cname, r.skill_id, r.name, r.trained, r.active, r.sp,
                             "" if r.cap is None else r.cap, "omega" if r.beyond_alpha else "alpha",
                             int(r.restricted), int(r.pending_completion), int(r.unknown_data)])
    return buf.getvalue()


def observation_from_ctx(ctx: dict) -> watchstate.CharacterObservation:
    """The persisted slice of one fetch: queue entries and settled levels only.
    Tokens stay in the ctx and never reach watchstate."""
    cid = ctx["token"]["character_id"]
    items = tuple(
        watchstate.QueueItem(
            skill_id=int(q["skill_id"]), finished_level=int(q.get("finished_level") or 0),
            status=queue_status(q, ctx["now"]), name=ctx["names"].get(int(q["skill_id"]), f"skill {int(q['skill_id'])}"),
            finish_date=q.get("finish_date"),
        )
        for q in ctx["queue"]
    )
    return watchstate.CharacterObservation(
        character_id=cid, character_name=ctx["public"].get("name") or str(cid),
        items=items, trained_levels={r.skill_id: r.trained for r in ctx["rows"]},
    )


def cmd_summary(args):
    ctxs, failures = gather_all(args)
    for line in failures:
        print(f"warning: skipped {line}", file=sys.stderr)
    if not ctxs:
        raise RuntimeError("no character data could be fetched")
    rows, total_sp = [], 0
    for ctx in ctxs:
        sp = int(ctx["skills_doc"].get("total_sp") or 0)
        total_sp += sp
        active = next((i for i in ctx["queue"] if queue_status(i, ctx["now"]) == "training"), None)
        left = render.format_duration((render.parse_ts(active["finish_date"]) - ctx["now"]).total_seconds()) if active else "-"
        rows.append([ctx["public"].get("name") or "?", str(ctx["token"]["character_id"]),
                     f"{ctx['state'].state} ({ctx['state'].confidence})", render.format_sp(sp), str(len(ctx["queue"])), left])
    rows.append(["TOTAL", "", f"{len(ctxs)} characters", render.format_sp(total_sp), "", ""])
    print(render.table(["character", "id", "clone state", "total SP", "queue items", "current item left"], rows))


def parse_target(spec: str) -> tuple[str, int]:
    name, sep, lvl = spec.rpartition(":")
    if sep and lvl.isdigit() and 1 <= int(lvl) <= 5 and name:
        return name.strip(), int(lvl)
    return spec.strip(), 5


def load_skill_catalog() -> dict[int, alphadata.SkillInfo]:
    """The SDE skill catalog, with a message saying what to do when it is not installed."""
    try:
        return alphadata.skill_catalog()
    except FileNotFoundError:
        raise RuntimeError("no local skill catalog - run: eve-skills update-data") from None


def resolve_skill_id(name: str, catalog: dict[int, alphadata.SkillInfo]) -> int:
    """Resolve a target against the whole catalog, so never-trained skills work too."""
    needle = name.strip().lower()
    exact = [s for s, info in catalog.items() if info.name.lower() == needle]
    matches = exact or [s for s, info in catalog.items() if needle in info.name.lower()]
    if not matches:
        raise RuntimeError(f"no skill named '{name}' in the local skill catalog "
                           f"({len(catalog)} skills) - refresh it with: eve-skills update-data")
    if len(matches) > 1 and exact:
        matches = [s for s in exact if catalog[s].published] or exact
    if len(matches) > 1:
        shown = ", ".join(sorted(catalog[s].name for s in matches)[:6])
        raise RuntimeError(f"'{name}' is ambiguous: {shown}")
    return matches[0]


def cmd_plan(args):
    if not args.char and len(sso.list_characters()) > 1:
        raise RuntimeError("plan needs --char (one character at a time)")
    ctx = gather(args)
    catalog = load_skill_catalog()
    targets: dict[int, int] = {}
    for spec in args.target:
        name, level = parse_target(spec)
        sid = resolve_skill_id(name, catalog)
        targets[sid] = max(targets.get(sid, 0), level)  # a repeated target means its deepest level
    plan = planner.build_plan(targets, catalog, {r.skill_id: r.trained for r in ctx["rows"]},
                              planner.scheduled_levels(ctx["queue"]))
    if args.rate is not None and args.rate <= 0:
        raise RuntimeError(f"--rate must be a positive SP/hour value, not {args.rate:g}")
    # A plan with no items needs no rate, so it must not be refused for want of one: "every target
    # is already covered" is the answer, and demanding --rate to print it would be an error message
    # standing in for good news. Every consumer of `rate` below is inside the items loop or guarded.
    if args.rate is not None:
        rate, rate_src = float(args.rate), "--rate override"
    elif plan.items:
        rate, rate_src = planner.calibrated_rate(ctx)
    else:
        rate, rate_src = None, None

    # New items can only start once everything already queued has finished.
    eta = ctx["now"]
    pending = [render.parse_ts(q["finish_date"]) for q in ctx["queue"]
               if q.get("finish_date") and queue_status(q, eta) != "done"]
    backlog_end = max(pending, default=None)
    if backlog_end and backlog_end > eta:
        eta = backlog_end

    name = ctx["public"].get("name") or ctx["token"]["character_id"]
    print(f"{name}: {len(plan.items)} item(s) to train"
          + (f", {len(plan.covered)} already covered" if plan.covered else ""))
    rows, notes = [], []
    for item in plan.items:
        hours = item.sp / rate
        eta += timedelta(hours=hours)
        why = "requested" if item.requested else "for " + ", ".join(item.required_by[:2]) + (
            f" +{len(item.required_by) - 2}" if len(item.required_by) > 2 else "")
        rows.append([item.name, why, f"L{item.from_level}" + ("*" if item.from_level > item.trained_level else ""),
                     f"L{item.to_level}", render.format_sp(item.sp), render.format_duration(hours * 3600),
                     eta.strftime("%b %d %H:%M")])
        cap = ctx["caps"].get(item.skill_id)
        if cap is None:
            notes.append(f"{item.name} is omega-only (absent from the alpha list)")
        elif item.to_level > cap:
            notes.append(f"{item.name} target L{item.to_level} exceeds its alpha cap {cap} - needs an omega clone to train")
        if not catalog[item.skill_id].published:
            notes.append(f"{item.name} is no longer published by CCP - its cost may be historical")

    if plan.items:
        print(render.table(["skill", "why", "now", "target", "SP cost", "time", "ready"], rows))
        print(f"\ntotal: {render.format_sp(plan.total_sp)} SP; queue would end ~{eta.strftime('%Y-%m-%d %H:%M')} UTC")
    if backlog_end and backlog_end > ctx["now"]:
        print(f"note: the existing queue drains first (finishes ~{backlog_end.strftime('%Y-%m-%d %H:%M')} UTC);"
              " new items start after that")
    for note in notes:
        print(f"note: {note}")
    for covered in plan.covered:
        how = (f"reaches L{covered.level} in the existing queue" if covered.via_queue
               else f"is already trained to L{covered.level}")
        print(f"note: {covered.name} {how} - its L{covered.target} requirement costs nothing")
    if any(item.from_level > item.trained_level for item in plan.items):
        print("note: * = an existing queue entry raises this skill to that level before the item starts")
    pairs = sorted({(catalog[i.skill_id].primary, catalog[i.skill_id].secondary) for i in plan.items})
    if rate is not None:
        print(f"rate: {rate:,.0f} SP/hour ({rate_src})")
    if len(pairs) > 1:
        listed = ", ".join(f"{primary}/{secondary}" for primary, secondary in pairs)
        print(f"note: one rate is applied to every row, but these skills are driven by {listed};"
              " rows whose attributes differ from the calibrated skill are estimates")
    print("note: costs are rank-based and exact; remaps or implant changes while training shift real time")


def cmd_extract(args):
    if not args.char and len(sso.list_characters()) > 1:
        raise RuntimeError("extract needs --char (one character at a time)")
    ctx = gather(args)
    total = int(ctx["skills_doc"].get("total_sp") or 0)
    unalloc = int(ctx["skills_doc"].get("unallocated_sp") or 0)
    allocated = total - unalloc
    try:
        rate, rate_src = planner.calibrated_rate(ctx)
    except RuntimeError:
        rate, rate_src = None, "unknown"
    p = planner.extraction_plan(allocated, total, rate)
    name = ctx["public"].get("name") or ctx["token"]["character_id"]
    print(f"{name}: {render.format_sp(allocated)} trained (allocated) SP, {render.format_sp(unalloc)} unallocated")
    if p["count"] == 0:
        print(f"cannot extract: {p['reason']}")
    else:
        print(f"can run {int(p['count'])} Skill Extractor(s) of {planner.EXTRACTION['extractor_sp']:,} SP each "
              f"(must keep {planner.EXTRACTION['floor_sp']:,} trained; queued/training SP is not extractable)")
        if p["retrain_days_each"]:
            print(f"each extractor costs about {p['retrain_days_each']:.1f} days of re-training at {rate:,.0f} SP/hour ({rate_src})")
        print(f"re-injecting here yields only {p['injector_value']:,} SP per injector at {render.format_sp(total)} total SP "
              f"(tiers: 500k below 5M, 400k to 50M, 300k to 80M, else 150k - lower-SP alts gain more)")
    warn = planner.extraction_rules_warning(ctx["now"])
    if warn:
        print("warning:", warn)
    else:
        print(f"rules per CCP ({planner.EXTRACTION['source']}), verified {planner.EXTRACTION['verified']}")


def cmd_skills(args):
    if args.watch:
        from . import cmd_watch   # local: cmd_watch imports this module at import time
        return cmd_watch.cmd_watch(args)
    ctxs, failures = gather_all(args)
    for line in failures:
        print(f"warning: skipped {line}", file=sys.stderr)
    if not ctxs:
        raise RuntimeError("no character data could be fetched")
    if args.csv:
        sys.stdout.write(render_csv(ctxs))
        return
    if args.json:
        docs = [json.loads(render_json(ctx, args)) for ctx in ctxs]
        print(json.dumps(docs[0] if len(docs) == 1 else docs, indent=2))
        return
    blocks = [render_text(ctx, args) + (("\n" + week_line(ctx)) if args.week else "") for ctx in ctxs]
    print(("\n" + "=" * 72 + "\n").join(blocks))
