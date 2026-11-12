"""One-screen board for planning the next industry jobs at one site of the operation file.

Usage (from the repo root; a source checkout runs `.venv/bin/python` instead of `python`):
    python .claude/skills/industry-jobs/job_board.py [--site NAME] [--start HH:MM] [--tz ZONE]
        [--finish-window 08-10,20-22] [--t1 invent|build|both] [--since HH:MM] [--days 14]
        [--max-hours 60] [--max-invention-hours 30] [--char NAME] [--all-blueprints] [--no-pool-check]

The site comes from the operation file (`eve-skills operation show`; `eve-skills operation template`
prints a starting point). A corp site reads the corporation through its readers; a personal site reads
each listed character's own slots, blueprints and stock at the site's facilities.

Collects in one pass what a job-planning session otherwise rebuilds by hand:
  * slots per character (manufacturing / science): used, ready (finished, not delivered), free, next end
  * jobs ready to deliver, and the running jobs by end time
  * idle blueprints by what they can do (`industry status`: `activities`, `invents`): copies to build,
    copies to invent from, originals; each row names its facility, and `haul to X first` when it sits
    at another facility than the one its activity runs in
  * measured hours per run where the site does the activity (`jobs --times --site`; `~` = converted
    from another facility) and, with finish windows, the run counts that end inside one
  * personal sites: whether the pool holder's stock covers the copies to build (`restock --site`) and
    every input still short; the `restock --invent` command for the invention copies (not run)
  * inputs per facility: total per item and who holds it (corp sites: per division)
  * with --since: what was installed since then (`jobs --since`)

Time zone and finish windows: the flags, else the operation file's `planning`, else UTC and no window
sizing (end times only). Every ESI document keeps its own cache: the header prints how old each is.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import subprocess
import sys

from eve_skills.job_times import zone as zone_named

# `industry status` activity names -> the operation file's activity keys
ACTIVITY_KEYS = {"manufacturing": "manufacturing", "invention": "invention", "copying": "copying",
                 "material efficiency research": "research_material",
                 "time efficiency research": "research_time", "reaction": "reaction"}
INPUT_CATEGORIES = {"Material", "Commodity", "Decryptors"}
T1_MODES = ("invent", "build", "both")


def cli(*args: str) -> dict | None:
    """`eve-skills ARGS --json` as a dict; None (and a note on stderr) when the command fails.

    The only place this script reaches the CLI (and through it ESI): tests replace it."""
    run = subprocess.run([sys.executable, "-m", "eve_skills", *args, "--json"],
                         capture_output=True, text=True)
    if run.returncode or not run.stdout.strip():
        print(f"! eve-skills {' '.join(args)} failed: {run.stderr.strip()[-300:]}", file=sys.stderr)
        return None
    return json.loads(run.stdout)


def parse(ts: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def parse_windows(spec: str | None) -> list[tuple[int, int]]:
    """"08-10,20-22" -> [(8, 10), (20, 22)]; a window may wrap midnight ("22-02")."""
    out = []
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        try:
            lo, hi = (int(x) for x in part.strip().split("-"))
        except ValueError:
            sys.exit(f"finish window '{part.strip()}' is not HH-HH")
        if not (0 <= lo <= 24 and 0 <= hi <= 24) or lo == hi:
            sys.exit(f"finish window '{part.strip()}' is not HH-HH")
        out.append((lo, hi))
    return out


class Clock:
    """Times shown in the planning zone, and whether a moment falls in a finish window."""

    def __init__(self, zone: str, windows: list[tuple[int, int]], now: dt.datetime):
        self.zone_name = zone
        self.zone = zone_named(zone)  # UTC and fixed offsets need no time zone database
        self.windows = windows
        self.now = now

    def show(self, ts: dt.datetime | None) -> str:
        if ts is None:
            return "-"
        local = ts.astimezone(self.zone)
        days = (local.date() - self.now.astimezone(self.zone).date()).days
        day = {0: "today", 1: "tmrw"}.get(days, local.strftime("%a"))
        return f"{day} {local:%H:%M}"

    def in_window(self, ts: dt.datetime) -> bool:
        if not self.windows:
            return True
        hour = ts.astimezone(self.zone).hour
        return any(lo <= hour < hi if lo < hi else (hour >= lo or hour < hi) for lo, hi in self.windows)


# --- the site --------------------------------------------------------------------------------------

def load_operation() -> dict:
    op = cli("operation")
    if not op or not op.get("configured") or not op.get("sites"):
        sys.exit("job_board plans a site of the operation file, and there is none: `eve-skills operation "
                 "template` prints a starting point, `eve-skills operation show` checks the file")
    return op


def load_site(op: dict, name: str | None) -> dict:
    """{name, owner, characters, readers, pool_holder, by_activity: {activity: facility key},
    facilities: {key: {label, location_id}}} for the named site, else the file's default site."""
    sites = op["sites"]
    key = (name or op.get("default_site") or "").lower()
    if not key and len(sites) == 1:
        key = next(iter(sites))
    if key not in sites:
        which = f"no site '{name}'" if name else "no default site"
        sys.exit(f"{which} in the operation file; pass --site NAME (sites: {', '.join(sites)})")
    raw = sites[key]
    overrides = raw.get("facilities") or {}
    by_activity = {act: overrides.get(act) or raw["facility"] for act in ACTIVITY_KEYS.values()}
    facilities = {}
    for fac in dict.fromkeys([raw["facility"], *overrides.values()]):
        info = (op.get("facilities") or {}).get(fac) or {}
        facilities[fac] = {"label": info.get("label") or fac, "location_id": info.get("location_id")}
    return {"name": key, "owner": raw["owner"], "characters": raw.get("characters") or [],
            "readers": raw.get("readers") or [], "pool_holder": raw.get("pool_holder"),
            "by_activity": by_activity, "facilities": facilities}


SHORT = {"manufacturing": "manufacturing", "invention": "invention", "copying": "copying",
         "research_material": "ME research", "research_time": "TE research"}


def facility_line(site: dict) -> str:
    groups = collections.defaultdict(list)
    for act, short in SHORT.items():
        groups[site["by_activity"][act]].append(short)
    return "; ".join(f"{'/'.join(acts)} at {site['facilities'][fac]['label']}" for fac, acts in groups.items())


# --- status: slots, jobs and blueprints ---------------------------------------------------------------

def read_status(site: dict, char: str | None) -> tuple[dict, str, list]:
    """(merged owner view, who it was read through, top-level warnings)."""
    if site["owner"] == "personal":
        status = cli("industry", "status")
        if status is None:
            sys.exit("eve-skills industry status failed")
        members = {c.casefold() for c in site["characters"]}
        owners = [o for o in status["owners"] if o["owner_name"].casefold() in members]
        missing = members - {o["owner_name"].casefold() for o in owners}
        view = {"characters": [c for o in owners for c in o["characters"]],
                "documents": [o.get("documents") or {} for o in owners],
                "warnings": [w for o in owners for w in (o.get("warnings") or [])],
                "adjustments": [(o["owner_name"], o.get("blueprint_adjustments") or {}) for o in owners],
                "jobs": {"ready": [j for o in owners for j in o["jobs"].get("ready", [])],
                         "running": [j for o in owners for j in o["jobs"].get("running", [])]},
                "blueprints": [dict(b, owner=o["owner_name"]) for o in owners for b in o["blueprints"]],
                "missing": sorted(missing)}
        return view, ", ".join(o["owner_name"] for o in owners) or "nobody", status.get("warnings") or []
    readers = [char] if char else list(site["readers"])
    status = cli("industry", "status", "--corp")
    wanted = {r.casefold() for r in readers}
    owner = None
    for o in (status or {}).get("owners") or []:
        if not wanted or wanted & {c["character_name"].casefold() for c in o["characters"]}:
            owner = o
            break
    for reader in readers if owner is None else ():
        status = cli("industry", "status", "--corp", "--char", reader)
        if status and status.get("owners"):
            owner = status["owners"][0]
            break
    if owner is None:
        sys.exit(f"could not read the corporation through {', '.join(readers) or 'any stored character'}")
    names = {c["character_id"]: c["character_name"] for c in owner["characters"]}
    view = {"characters": owner["characters"], "documents": [owner.get("documents") or {}],
            "warnings": owner.get("warnings") or [],
            "adjustments": [(owner["owner_name"], owner.get("blueprint_adjustments") or {})],
            "jobs": owner["jobs"], "blueprints": owner["blueprints"], "missing": [],
            "corporation": owner["owner_name"]}
    reader = names.get(owner.get("read_by_character_id")) or (readers[0] if readers else owner["owner_name"])
    return view, reader, status.get("warnings") or []


def warning_lines(top: list, view: dict) -> list[str]:
    seen: list[str] = []
    delivered = collections.Counter()  # subject -> jobs, summed over the owners read
    for w in list(top) + list(view["warnings"]):
        if isinstance(w, dict) and "delivered_jobs" in w and not w.get("message"):
            delivered[w["subject"]] += w["delivered_jobs"]
            continue
        text = w["message"] if isinstance(w, dict) and w.get("message") else str(w)
        if text not in seen:
            seen.append(text)
    for subject, count in sorted(delivered.items()):
        seen.append(f"{count} jobs delivered after the {subject} snapshot: their output is not in these numbers")
    for owner, adj in view["adjustments"]:
        if (adj.get("dropped") or adj.get("reduced")) and not any("blueprint snapshot" in s and "cop" in s for s in seen):
            seen.append(f"{owner}: {adj.get('dropped', 0)} copies dropped and {adj.get('reduced', 0)} reduced "
                        "(used by jobs delivered after the blueprint snapshot)")
    return seen


def document_ages(view: dict, now: dt.datetime) -> list[str]:
    """One line per document kind: the oldest copy across the owners read."""
    oldest: dict[str, dt.datetime] = {}
    for docs in view["documents"]:
        for name, doc in docs.items():
            lm = parse((doc or {}).get("last_modified"))
            if lm and (name not in oldest or lm < oldest[name]):
                oldest[name] = lm
    return [f"{name}: data from {lm:%H:%M} UTC ({max(0, (now - lm).total_seconds()) / 60:.0f} min old)"
            for name, lm in sorted(oldest.items())]


# --- measured times ------------------------------------------------------------------------------------

def measured(site: dict, clock: Clock, days: int, start: dt.datetime, max_hours: float) -> dict:
    """(activity, product, installer) -> (h/run, jobs, [(runs, end)], scaled) from `jobs --times --site`.

    Invention and copying products come back as blueprints; the ' Blueprint' suffix is dropped so every
    activity keys on the item's name."""
    args = ["jobs", "--times", "--site", site["name"], "--days", str(days)]
    if clock.windows:
        args += ["--finish-window", ",".join(f"{lo:02d}-{hi:02d}" for lo, hi in clock.windows),
                 "--tz", clock.zone_name, "--start", start.isoformat(), "--max-hours", str(max_hours)]
    out = {}
    for doc in (cli(*args) or {}).get("documents", []):
        for r in doc.get("times", []):
            key = (r["activity"], r["product"].removesuffix(" Blueprint"), r["installer"])
            fits = [(f["runs"], parse(f["end"])) for f in r.get("fits") or []]
            out.setdefault(key, (r["hours_per_run"], r["jobs"], fits, bool(r.get("scaled_from"))))
    return out


# --- blueprints ----------------------------------------------------------------------------------------

def classify(bps: list[dict], t1_mode: str, has_time, never: set[str], show_all: bool) -> dict:
    """Blueprint rows by section: build, invent, originals; plus what was hidden and why.

    A copy that can only manufacture (an invented copy, say) is always shown. A copy that can both
    build and invent goes where `t1_mode` puts it. Rows that can do more than that are shown when
    their line has a measured time for the activity in the history window, or with `show_all`."""
    out = {"build": [], "invent": [], "originals": [], "hidden": collections.Counter(), "never": set()}
    for b in bps:
        acts = set(b.get("activities") or [])
        product = b.get("product") or b.get("blueprint") or "?"
        if b["kind"] == "BPO":
            if show_all or has_time("manufacturing", product) or has_time("copying", product):
                out["originals"].append(b)
            else:
                out["hidden"]["originals"] += 1
            continue
        can_build = "manufacturing" in acts
        can_invent = "invention" in acts and bool(b.get("invents"))
        targets = []
        if can_build and can_invent:
            targets = [s for s, mode in (("build", "build"), ("invent", "invent")) if t1_mode in (mode, "both")]
        elif can_build:
            targets = ["build"]
        elif can_invent:
            targets = ["invent"]
        if not targets:
            out["hidden"]["other copies"] += 1
            continue
        for section in targets:
            if section == "build":
                if product.casefold() in never:
                    out["never"].add(product)
                elif show_all or not can_invent or has_time("manufacturing", product):
                    out["build"].append(b)
                else:
                    out["hidden"]["copies to build"] += 1
            else:
                lines = [p for p in b["invents"] if p.casefold() not in never]
                out["never"].update(p for p in b["invents"] if p.casefold() in never)
                if not lines:
                    continue
                if show_all or any(has_time("invention", p) for p in lines):
                    out["invent"].append(dict(b, invents=lines))
                else:
                    out["hidden"]["copies to invent from"] += 1
    return out


def where(b: dict, site: dict, activity: str, by_location: dict) -> str:
    """'<facility label>' plus 'haul to X first' when the row is not where `activity` runs."""
    fac = by_location.get(b.get("location_id"))
    here = site["facilities"][fac]["label"] if fac else (b.get("location") or "?")
    target = site["by_activity"].get(ACTIVITY_KEYS.get(activity, activity))
    if fac != target and target in site["facilities"]:
        return f"{here}; haul to {site['facilities'][target]['label']} first"
    return here


# --- stock ---------------------------------------------------------------------------------------------

def read_stock(site: dict, reader: str, by_location: dict) -> dict:
    """{facility key: {item: {holder: qty}}} for inputs at the site's facilities, plus deliveries.

    Holder = character (personal site) or `div N` (corp site). Items in a structure's Deliveries hangar
    are kept apart: a job reads only the installer's item hangar."""
    stock = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    groups: dict[str, str] = {}
    deliveries = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    sources = ([(who, ["inventory", "--char", who, "--items"]) for who in site["characters"]]
               if site["owner"] == "personal" else [(None, ["inventory", "--corp", "--char", reader, "--items"])])
    for who, args in sources:
        data = cli(*args) or {}
        for it in ((data.get("characters") or [{}])[0]).get("items", []):
            if it.get("category_name") not in INPUT_CATEGORIES:
                continue
            fac = next((by_location[p["id"]] for p in it.get("location_path") or [] if p.get("id") in by_location), None)
            if fac is None:
                continue
            holder = who or f"div {it.get('division_number') or it.get('division') or '?'}"
            groups[it["type_name"]] = it.get("group_name") or ""
            stock[fac][it["type_name"]][holder] += it["quantity"]
            if it.get("flag") == "Deliveries":
                deliveries[fac][holder][it["type_name"]] += it["quantity"]
    return {"stock": stock, "groups": groups, "deliveries": deliveries}


def pool_holder(site: dict, stock: dict) -> str | None:
    if site["pool_holder"]:
        return site["pool_holder"]
    at_build = stock["stock"].get(site["by_activity"]["manufacturing"]) or {}
    totals = collections.Counter()
    for holders in at_build.values():
        totals.update(holders)
    return totals.most_common(1)[0][0] if totals else None


# --- the board -----------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", help="site of the operation file (default: its default site)")
    ap.add_argument("--char", help="corp reader on a corp site (default: the site's readers)")
    ap.add_argument("--tz", help="planning time zone (default: planning.timezone, else UTC)")
    ap.add_argument("--finish-window", help="hours a job should end in, e.g. 08-10,20-22 "
                                            "(default: planning.finish_windows; none = end times only)")
    ap.add_argument("--start", help="planned install time HH:MM in the planning zone (default: now + 10 min)")
    ap.add_argument("--t1", choices=T1_MODES,
                    help="where a copy that can build and invent is listed (default: planning.t1_copies)")
    ap.add_argument("--since", help="also list jobs installed since HH:MM or an ISO time (jobs --since)")
    ap.add_argument("--max-hours", type=float, default=60, help="longest manufacturing job to propose (default 60)")
    ap.add_argument("--max-invention-hours", type=float, default=30,
                    help="longest invention job to propose (default 30)")
    ap.add_argument("--days", type=int, default=14, help="history window for measured run times (default 14)")
    ap.add_argument("--all-blueprints", action="store_true",
                    help="list every idle blueprint, also lines with no job in the history window")
    ap.add_argument("--no-pool-check", action="store_true", help="personal site: skip the restock check")
    args = ap.parse_args(argv)

    op = load_operation()
    planning = op.get("planning") or {}
    site = load_site(op, args.site)
    personal = site["owner"] == "personal"
    now = dt.datetime.now(dt.timezone.utc)
    zone = args.tz or planning.get("timezone") or "UTC"
    try:
        clock = Clock(zone, parse_windows(args.finish_window or planning.get("finish_windows")), now)
    except Exception as err:  # an unknown zone name
        sys.exit(f"time zone '{zone}': {err}")
    if args.start:
        try:
            hh, mm = map(int, args.start.split(":"))
            local = now.astimezone(clock.zone).replace(hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            sys.exit(f"--start '{args.start}' is not HH:MM")
        if local < now.astimezone(clock.zone) - dt.timedelta(hours=1):
            local += dt.timedelta(days=1)
        start = local.astimezone(dt.timezone.utc)
    else:
        start = now + dt.timedelta(minutes=10)
    never = {p.casefold() for p in planning.get("never_build") or []}
    t1_mode = args.t1 or planning.get("t1_copies") or "both"
    by_location = {f["location_id"]: key for key, f in site["facilities"].items() if f["location_id"]}

    view, reader, top_warnings = read_status(site, args.char)
    print(f"# Job board {now:%Y-%m-%d %H:%M} UTC = {clock.show(now)} {zone}; install assumed {clock.show(start)}"
          f" - site {site['name']} ({site['owner']}), read via {reader}")
    print(f"  {facility_line(site)}")
    for line in document_ages(view, now):
        print(f"  {line}")
    if view["missing"]:
        print(f"  warning: not readable (not stored, or no consent): {', '.join(view['missing'])}")
    for key, fac in site["facilities"].items():
        if not fac["location_id"]:
            print(f"  warning: facility {key} has no location_id in the operation file: its blueprints and stock"
                  " cannot be placed and are left out")
    for text in warning_lines(top_warnings, view):
        print(f"  warning: {text}")

    # --- slots
    print("\n## Slots (used/max, ready = finished but not delivered)")
    print(f"{'character':22} {'mfg':>7} {'ready':>5} {'free':>4} {'next end':>12}   "
          f"{'sci':>7} {'ready':>5} {'free':>4} {'next end':>12}")
    for c in view["characters"]:
        cells = []
        for kind in ("manufacturing", "science"):
            x = c["slots"].get(kind) or {}
            cells += [f"{x.get('used', 0)}/{x.get('max', 0)}", x.get("ready", 0), x.get("free", 0),
                      clock.show(parse(x.get("next_end")))]
        print(f"{c['character_name']:22} {cells[0]:>7} {cells[1]:>5} {cells[2]:>4} {cells[3]:>12}   "
              f"{cells[4]:>7} {cells[5]:>5} {cells[6]:>4} {cells[7]:>12}")

    # --- jobs
    jobs = view["jobs"]
    if jobs.get("ready"):
        print("\n## Ready to deliver (these hold slots until delivered)")
        for j in jobs["ready"]:
            print(f"  {j['installer_name']:20} {j['activity']:14} {j['count']}x {j['product']} ({j['runs']})")
    print(f"\n## Running, by end ({zone})")
    for j in sorted(jobs.get("running", []), key=lambda j: j["end_start"]):
        end = parse(j["end_start"])
        last = parse(j.get("end_end")) or end
        flag = "" if clock.in_window(end) and clock.in_window(last) else "  (outside window)"
        until = f" (last ends {clock.show(last)})" if clock.show(last) != clock.show(end) else ""
        print(f"  {clock.show(end):>11}  {j['installer_name']:20} {j['activity']:13} {j['count']:>2}x "
              f"{j['product']} ({j['runs']}){until}{flag}")

    if args.since:
        since = cli("jobs", "--since", args.since, "--site", site["name"], "--tz", zone)
        print(f"\n## Installed since {(since or {}).get('since') or args.since}")
        rows = [r for doc in (since or {}).get("documents", []) for r in doc.get("jobs", [])]
        for r in sorted(rows, key=lambda r: (r["start"], r["installer"])):
            print(f"  {clock.show(parse(r['start'])):>11}  {r['installer']:20} {r['activity']:13} {r['count']:>2}x "
                  f"{r['product']} ({r['runs']}) at {r['facility']}, {r['status']}")
        if since is not None and not rows:
            print("  (none)")

    # --- measured times and window fits
    times = measured(site, clock, args.days, start, max(args.max_hours, args.max_invention_hours))
    timed = {(a, p) for a, p, _who in times}

    def has_time(activity: str, product: str) -> bool:
        return (activity, product) in timed

    missing_time = False

    def sizing(activity: str, product: str, max_runs: int) -> str:
        nonlocal missing_time
        rows = {who: v for (a, p, who), v in times.items() if (a, p) == (activity, product)}
        if not rows:
            missing_time = True
            return "-"
        cap = args.max_invention_hours if activity == "invention" else args.max_hours
        parts = []
        for who, (h, _n, all_fits, scaled) in sorted(rows.items(), key=lambda kv: kv[1][0]):
            mark = "~" if scaled else ""
            full = min(max_runs, max(1, int(cap // h)))
            if clock.windows:
                fits = [(n, end) for n, end in all_fits if n <= max_runs and n * h <= cap][:2]
                tail = (" or ".join(f"{n} runs -> {clock.show(end)}" for n, end in fits) if fits else
                        f"no window fit ({full} runs -> {clock.show(start + dt.timedelta(hours=full * h))})")
            else:
                tail = f"{full} runs -> {clock.show(start + dt.timedelta(hours=full * h))}"
            parts.append(f"{who} {mark}{h:.2f}h: {tail}")
        return "; ".join(parts)

    # --- blueprints at the site's facilities
    at_site = [b for b in view["blueprints"] if not by_location or b.get("location_id") in by_location]
    elsewhere = len(view["blueprints"]) - len(at_site)
    sections = classify(at_site, t1_mode, has_time, never, args.all_blueprints)

    def holder(b: dict) -> str:
        return b.get("owner") or f"div {b.get('division_number')}"

    print("\n## Copies to build - count x runs, ME/TE, holder, facility; sizing from measured h/run")
    for b in sorted(sections["build"], key=lambda b: (b["product"], -(b["runs_per_job"] or 0))):
        limit = ""
        if not personal:  # a corp hangar check; on a personal site it reads only the copy's own hangar
            limit = f"  buildable jobs {b.get('can_build_jobs')}"
            if b.get("limiting_material") and not b.get("can_build_jobs"):
                limit += f"; limit {b['limiting_material']} {b['have']}/{b['per_job']}"
        print(f"  {b['count']:>3} x {b['runs_per_job']:>3}r {b['product']:40} ME{b['me']}/TE{b['te']} {holder(b)}"
              f"  [{where(b, site, 'manufacturing', by_location)}]{limit}")
        print(f"        {sizing('manufacturing', b['product'], b['runs_per_job'] or 1)}")
    if not sections["build"]:
        print("  (none)")
    print("\n## Copies to invent from (attempts <= runs on the copy)")
    for b in sorted(sections["invent"], key=lambda b: (b["product"], -(b["runs_per_job"] or 0))):
        for product in b["invents"]:
            print(f"  {b['count']:>3} x {b['runs_per_job']:>3}r {b['product']:40} {holder(b)}"
                  f"  [{where(b, site, 'invention', by_location)}] -> {product}")
            print(f"        {sizing('invention', product, b['runs_per_job'] or 1)}")
    if not sections["invent"]:
        print("  (none)")
    if sections["originals"]:
        print("\n## Originals (copy or build) - ME/TE, holder, facility")
        for b in sorted(sections["originals"], key=lambda b: b["product"] or ""):
            acts = ", ".join(a for a in b.get("activities") or [] if a in ("manufacturing", "copying"))
            print(f"  {b['count']:>3} x {b['product']:44} ME{b['me']}/TE{b['te']} {holder(b)}"
                  f"  [{where(b, site, 'copying', by_location)}] ({acts})")
            print(f"        copy: {sizing('copying', b['product'], 1_000)}")
    hidden = ", ".join(f"{n} {what}" for what, n in sorted(sections["hidden"].items()))
    if hidden:
        print(f"\n({hidden} hidden: no job for their line in the last {args.days} days; --all-blueprints lists them)")
    if elsewhere:
        print(f"({elsewhere} blueprint rows are held away from this site's facilities)")
    if sections["never"]:
        print(f"(left out by planning.never_build: {', '.join(sorted(sections['never']))})")

    # --- stock, then the pool check (personal) or the restock command (corp)
    stock = read_stock(site, reader, by_location) if by_location else {"stock": {}, "groups": {}, "deliveries": {}}
    units = collections.Counter()
    for b in sections["build"]:
        units[(b["product"], b["me"], b["runs_per_job"])] += b["count"] * (b["runs_per_job"] or 1)
    attempts = collections.Counter()
    inventors: dict[str, str] = {}
    for b in sections["invent"]:
        for product in b["invents"]:
            attempts[product] += b["count"] * (b["runs_per_job"] or 1)
            inventors.setdefault(product, b.get("owner") or "")
    site_flag = f"--site {site['name']}"
    if units:
        pool = pool_holder(site, stock) if personal else None
        lines = [f"{p}={u}:{me}:{runs}" + (f"@{pool}" if pool else "")
                 for (p, me, runs), u in sorted(units.items(), key=lambda kv: [str(x) for x in kv[0]])]
        command = f"restock {site_flag} " + " ".join(f'"{line}"' for line in lines)
        if personal and not args.no_pool_check:
            print(f"\n## Pool check: every copy to build, installed by {pool or '(no pool holder found)'}")
            data = cli("restock", "--site", site["name"], "--no-price-check", *lines) if pool else None
            for line in (data or {}).get("lines", []):
                print(f"  covered {line['fit']}/{line['units']}  {line['label']}  ({line['status']})")
            short = [m for m in (data or {}).get("materials", []) if m.get("buy", 0) > 0]
            if short:
                print("  short (need / on site / short):")
                for m in sorted(short, key=lambda m: m["name"]):
                    print(f"    {m['name']:40} {m['need']:>8,} {m['on_site']:>8,} {m['buy']:>8,}")
            elif data is not None:
                print("  nothing short")
            if data and data.get("transfers"):
                print(f"  {len(data['transfers'])} trades to the installer first: `eve-skills {command}` lists them")
            if not pool:
                print("  no stock at the manufacturing facility to pick a pool holder from; set sites."
                      f"{site['name']}.pool_holder")
        else:
            print("\n## restock lines for the copies to build")
            print(f"  {command}")
    if attempts:
        invent = " ".join(f'--invent "{p}={n}' + (f'@{inventors[p]}' if personal and inventors[p] else "") + '"'
                          for p, n in sorted(attempts.items()))
        print("\n## Invention inputs (attempts = every run on the copies; trim to the plan before running)")
        print(f"  restock {site_flag} {invent}")

    for fac, items in stock["stock"].items():
        label = site["facilities"][fac]["label"]
        how = "per character (a job reads only the installer's item hangar)" if personal else "by division"
        print(f"\n## Inputs at {label}, {how}")
        for name in sorted(items, key=lambda n: (stock["groups"].get(n) == "Mineral", n)):
            cells = ", ".join(f"{who}: {q:,}" for who, q in sorted(items[name].items(), key=str))
            print(f"  {name:42} {sum(items[name].values()):>10,}  {cells}")
        for who, parked in sorted((stock["deliveries"].get(fac) or {}).items()):
            held = sum(1 for holders in items.values() if who in holders)
            names = f" ({', '.join(sorted(parked))})" if len(parked) <= 6 and len(parked) < held else ""
            print(f"  {who}: ESI lists {len(parked)} of these {held} items in the Deliveries hangar{names}; "
                  "a job reads only the item hangar: move them before installing")
    if stock["stock"]:
        print("  (assets are cached ~1h: stock taken by jobs installed in the last hour may still show)")

    if missing_time:
        print(f"\n- = no job for this line and activity in the last {args.days} days; "
              f"`eve-skills build-cost ITEM {site_flag}` gives the formula time (skills shorten it)")
    if clock.windows:
        spans = ", ".join(f"{lo:02d}-{hi:02d}" for lo, hi in clock.windows)
        print(f"Windows: {spans} {zone}. 'no window fit' = shorten or extend by hand, or accept an off-window end.")
    else:
        print("No finish windows set (--finish-window or planning.finish_windows): end times only.")


if __name__ == "__main__":
    main()
