"""`ledger`: the accounting ledger's command line - sync, pnl, products, invention, inventory, and the
operation's log: note and progress.

`sync` and `note add|done` are the only subcommands that write; every report replays the stored facts (`ledger.replay`)
and so works offline, except `inventory`, which reads today's assets and Jita prices live because
"what is it worth" is a question about now. Names come through the shared name cache.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from datetime import datetime, timezone

from . import alphadata, esi as esi_mod, exports, industry, ledger, ledger_db, ledger_sync, market, sso
from . import render

NOTES = {
    "pre_ledger_copy_runs": "{n:,} blueprint-copy runs came from copies made before the ledger started; "
                            "those runs are costed at 0",
    "unknown_me": "{n:,} manufacturing jobs used a blueprint of unknown ME, costed at ME 0 "
                  "(materials may be overstated)",
    "me_from_sibling_copies": "{n:,} manufacturing jobs took their ME from other copies of the same blueprint",
    "me_assumed_invented": "{n:,} manufacturing jobs used a T2 copy no sync saw; costed at ME 2, "
                           "a copy invented without a decryptor",
    "unknown_decryptor": "{n:,} invention jobs: no invented copy seen yet, so the decryptor is unknown and "
                         "not costed - sync while the copies are still in a hangar",
    "decryptor_from_runs": "{n:,} invention jobs: decryptor identified from the runs manufacturing drew "
                           "from one copy (no copy was seen by a sync)",
    "ambiguous_decryptor": "{n:,} invention jobs: the copies fit two decryptors and neither was bought; "
                           "the first was assumed",
    "fees_from_job_row": "{n:,} jobs have no fee left in the journal; ESI's job cost was used instead",
    "structure_jobs": "{n:,} jobs ran in a player structure; its material bonuses are not modelled",
    "missing_recipe": "{n:,} jobs have no recipe in the local SDE data - run: eve-skills update-data",
    "missing_invention_recipe": "{n:,} invention jobs have no invention recipe in the local SDE data - "
                                "run: eve-skills update-data",
    "unpriced_opening_units": "{n:,} units drawn from opening stock have no market price and are costed "
                              "at 0 - profit on them is overstated",
    "internal_transfers": "{n:,} transactions between the business's own wallets were left out",
    "broker_fees_matched_by_rate": "{n:,} broker fees were matched to a later-modified order by their "
                                   "issuer's fee rate",
    "invention_without_outcome": "{n:,} delivered invention jobs carry no outcome yet",
}


def _compact(value: float | None) -> str:
    """ISK in the shorthand traders read: 1.23B, 45.6M, 789k."""
    if value is None:
        return "-"
    size = abs(value)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if size >= limit:
            return f"{value / limit:,.2f}{suffix}"
    return f"{value:,.0f}"


def _client() -> esi_mod.Esi:
    return esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))


def _open() -> sqlite3.Connection:
    if not ledger_db.exists():
        raise RuntimeError("no ledger yet - run: eve-skills ledger sync")
    return ledger_db.connect()


def _reference_data():
    try:
        recipes = industry.recipes_by_blueprint(alphadata.blueprint_materials())
    except FileNotFoundError:
        recipes = {}
    try:
        invention = alphadata.blueprint_invention()
    except FileNotFoundError:
        invention = {"blueprints": {}, "decryptors": {}}
    return recipes, invention


def _names(client: esi_mod.Esi | None, ids) -> dict[int, str]:
    wanted = {int(i) for i in ids if i is not None}
    if not wanted or client is None:
        return {}
    try:
        return esi_mod.resolve_names(client, wanted)
    except esi_mod.EsiError:
        return {}     # names are decoration; an outage must not cost the report


def _name(names: dict[int, str], ident) -> str:
    return "-" if ident is None else names.get(int(ident), f"type {ident}")


def _state(conn):
    """Replay everything: (book, header, recipes, invention). The header - cutover, last sync,
    warnings and the assumptions the replay made - is what every report opens with."""
    recipes, invention = _reference_data()
    last = ledger_db.last_sync(conn)
    now = last or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    book = ledger.replay(ledger.load(conn), recipes, invention, now)
    header = {"cutover": ledger_db.cutover(conn), "last_sync": last, "warnings": [],
              "notes": [NOTES.get(key, key + ": {n}").format(n=n) for key, n in sorted(book.notes.items())],
              "note_counts": dict(book.notes)}
    if not recipes:
        header["warnings"].append("blueprint recipes unavailable - run: eve-skills update-data")
    if not invention["blueprints"]:
        header["warnings"].append("invention data unavailable - run: eve-skills update-data")
    age = sync_age_days(last)
    if age is None:
        header["warnings"].append("the ledger has never synced - run: eve-skills ledger sync")
    elif age > ledger_db.STALE_SYNC_DAYS:
        header["warnings"].append(
            f"last sync {age:.0f} days ago - ESI keeps {ledger_db.WALLET_HISTORY_DAYS} days of wallet history, "
            f"so run: eve-skills ledger sync")
    return book, header, recipes, invention


def sync_age_days(last: str | None, now: datetime | None = None) -> float | None:
    if not last:
        return None
    moment = now or datetime.now(timezone.utc)
    return (moment - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() / 86400


def _print_header(title: str, header: dict) -> list[str]:
    lines = [f"{title} - cutover {header['cutover'] or '?'}, last sync {header['last_sync'] or 'never'}"]
    lines += [f"warning: {w}" for w in header["warnings"]]
    return lines


def _print_notes(lines: list[str], header: dict) -> None:
    if header["notes"]:
        lines.append("")
        lines.append("assumptions:")
        lines += [f"  - {note}" for note in header["notes"]]


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

def cmd_sync(args) -> int:
    client = _client()
    with contextlib.closing(ledger_db.connect()) as conn:
        report = ledger_sync.sync(conn, client, character=args.char, personal=not args.no_personal)
        if args.json:
            print(json.dumps(report.to_json(), indent=2))
            return 0
        rows = [[s.source, f"{s.seen:,}", f"{s.new:,}", s.oldest or "-", s.newest or "-"]
                for s in report.sources if s.seen or s.new]
        lines = [f"ledger synced at {report.synced_at} into {ledger_db.db_path(create=False)}"]
        if rows:
            lines.append(render.table(["source", "rows read", "new", "oldest", "newest"], rows))
        quiet = sum(1 for s in report.sources if not s.seen)
        if quiet:
            lines.append(f"({quiet} documents were empty)")
        lines.append(f"new rows: {sum(s.new for s in report.sources):,}; reference prices stored: {report.prices:,}; "
                     f"opening prices fixed: {report.opening_prices:,} (cutover {report.cutover or '?'})")
        lines += [f"warning: {ledger_sync.problem_text(p)}" for p in report.problems]
        print("\n".join(lines))
        return 0


# ---------------------------------------------------------------------------
# pnl
# ---------------------------------------------------------------------------

_STATEMENT = (("revenue", "revenue"), ("sales tax", "sales_tax"), ("broker fees", "broker_fees"),
              ("cost of goods sold", "cogs"), ("  of which opening stock at market", "cogs_opening_stock"),
              ("gross profit", "gross_profit"))


def cmd_pnl(args) -> int:
    with contextlib.closing(_open()) as conn:
        book, header, _recipes, _inv = _state(conn)
        # Journal rows reach back further than the trades and jobs that fix the cutover; before it the
        # ledger cannot cost anything, so a default statement must not collect overhead from there.
        args.since = args.since or header["cutover"]
        rows = ledger.pnl(book, since=args.since, until=args.until, by=args.by)
        if args.json:
            print(json.dumps({**header, "since": args.since, "until": args.until, "by": args.by,
                              "periods": rows}, indent=2))
            return 0
        lines = _print_header("ledger profit and loss", header)
        if not rows:
            lines.append("nothing booked in that period")
        elif args.by == "total":
            row = rows[0]
            lines.append(f"period {row['from'][:10]} .. {row['to'][:10]}")
            table = [[label, _compact(row[ledger.SCOPE_INVENTION][key]), _compact(row[ledger.SCOPE_OTHER][key]),
                      _compact(row["total"][key])] for label, key in _STATEMENT]
            for label, amount in row["overhead"].items():
                table.append([f"overhead: {label}", "", "", _compact(amount)])
            table.append(["net profit", "", "", _compact(row["net_profit"])])
            lines.append(render.table(["", "invention lines", "other", "total"], table))
            if row["revenue_personal_wallets"]:
                lines.append(f"revenue booked through personal wallets: {_compact(row['revenue_personal_wallets'])}")
        else:
            table = [[r["period"], _compact(r[ledger.SCOPE_INVENTION]["revenue"]),
                      _compact(r[ledger.SCOPE_INVENTION]["gross_profit"]), _compact(r["total"]["revenue"]),
                      _compact(r["total"]["gross_profit"]), _compact(r["overhead_total"]), _compact(r["net_profit"])]
                     for r in rows]
            lines.append(render.table(["period", "invention revenue", "invention gross", "total revenue",
                                       "total gross", "overhead", "net profit"], table))
        _print_notes(lines, header)
        print("\n".join(lines))
        return 0


# ---------------------------------------------------------------------------
# products
# ---------------------------------------------------------------------------

def cmd_products(args) -> int:
    with contextlib.closing(_open()) as conn:
        book, header, _recipes, _inv = _state(conn)
        rows = ledger.products(book, since=args.since, until=args.until, scope=args.scope)
        names = _names(_client(), [r["type_id"] for r in rows])
        for r in rows:
            r["type_name"] = names.get(r["type_id"])
        if args.limit:
            rows = rows[:args.limit]
        if args.json:
            print(json.dumps({**header, "since": args.since, "until": args.until, "products": rows}, indent=2))
            return 0
        lines = _print_header("ledger products", header)
        table = [[_name(names, r["type_id"]), "invention" if r["scope"] == ledger.SCOPE_INVENTION else "other",
                  f"{r['built']:,.0f}", _compact(r["unit_cost"]), f"{r['sold']:,.0f}", _compact(r["net_per_unit"]),
                  _compact(r["revenue"]), _compact(r["profit"]),
                  "-" if r["margin_pct"] is None else f"{r['margin_pct']:.0f}%"]
                 for r in rows]
        lines.append(render.table(["product", "line", "built", "unit cost", "sold", "net/unit", "revenue",
                                   "profit", "margin"], table) if table else "nothing built or sold in that period")
        lines.append("unit cost = materials + blueprint (invention or copy) + job fees of what was built; "
                     "profit = sales less tax, broker fees and the average cost of the units sold")
        _print_notes(lines, header)
        print("\n".join(lines))
        return 0


# ---------------------------------------------------------------------------
# invention
# ---------------------------------------------------------------------------

def cmd_invention(args) -> int:
    with contextlib.closing(_open()) as conn:
        book, header, recipes, invention = _state(conn)
        rows = ledger.invention(book, invention, recipes)
        names = _names(_client(), [r["product_type_id"] or r["blueprint_type_id"] for r in rows])
        for r in rows:
            r["product_name"] = names.get(r["product_type_id"] or r["blueprint_type_id"])
        if args.json:
            print(json.dumps({**header, "invention": rows}, indent=2))
            return 0
        lines = _print_header("ledger invention", header)
        table = [[_name(names, r["product_type_id"] or r["blueprint_type_id"]), r["decryptor"] or "unknown",
                  f"{r['attempts_done']}/{r['attempts']}", str(r["successes"]),
                  "-" if r["success_rate"] is None else f"{r['success_rate'] * 100:.0f}%",
                  _compact(r["cost_per_attempt"]), f"{r['runs_made']:,.0f}", _compact(r["cost_per_run"]),
                  _compact(r["cost_running"])]
                 for r in rows]
        lines.append(render.table(["product", "decryptor", "attempts done/all", "successes", "rate",
                                   "cost/attempt", "runs made", "cost/run", "in progress"], table)
                     if table else "no invention jobs stored")
        lines.append("cost/run spreads every attempt's cost, failures included, over the copy runs invented")
        _print_notes(lines, header)
        print("\n".join(lines))
        return 0


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------

def _live_assets(client: esi_mod.Esi, personal: bool) -> tuple[dict[int, int], list[str]]:
    """Quantity per type across the corporations' assets and, optionally, their members' own."""
    totals: dict[int, int] = {}
    problems: list[str] = []
    members: dict[int, list[dict]] = {}
    for rec in sso.list_characters():
        cid = int(rec["character_id"])
        try:
            tok = sso.get_access_token(cid)
            corp = exports.corp_of(client.get(f"/characters/{cid}"))
        except (RuntimeError, esi_mod.EsiError) as err:
            problems.append(f"character {cid}: {err}")
            continue
        if corp is not None and corp not in ledger_sync.NPC_CORPORATIONS:
            members.setdefault(corp, []).append(tok)

    def add(rows):
        for row in rows:
            if row.get("is_blueprint_copy"):
                continue
            totals[int(row["type_id"])] = totals.get(int(row["type_id"]), 0) + int(row.get("quantity") or 0)

    for corp, toks in members.items():
        for tok in toks:
            if "esi-assets.read_corporation_assets.v1" not in set(tok.get("scopes") or []):
                continue
            try:
                add(client.get_all(f"/corporations/{corp}/assets", token=tok["access_token"]))
                break
            except esi_mod.AuthError:
                continue
        else:
            problems.append(f"corporation {corp}: no member could read its assets "
                            f"(needs login --scopes assets and the Director role)")
        if personal:
            for tok in toks:
                if "esi-assets.read_assets.v1" in set(tok.get("scopes") or []):
                    try:
                        add(client.get_all(f"/characters/{tok['character_id']}/assets",
                                           token=tok["access_token"]))
                    except esi_mod.EsiError as err:
                        problems.append(f"{tok.get('character_name')}: {err}")
    return totals, problems


def cmd_inventory(args) -> int:
    with contextlib.closing(_open()) as conn:
        book, header, recipes, invention = _state(conn)
        client = _client()
        blueprint_types = set(recipes) | {int(k) for k in invention["blueprints"]} | {
            int(p[0]) for row in invention["blueprints"].values() for p in row["p"]}
        tracked = ledger_sync.relevant_types(conn) - blueprint_types
        finished = {j.product_type_id for j in book.jobs.values()
                    if j.activity_id in (ledger.MANUFACTURING, *ledger.REACTIONS) and j.product_type_id}
        held, problems = _live_assets(client, personal=not args.no_personal)
        listed: dict[int, int] = {}
        for row in conn.execute("SELECT type_id, volume_remain FROM orders WHERE is_buy = 0 AND state = 'open'"):
            if row[0] in tracked:
                listed[row[0]] = listed.get(row[0], 0) + int(row[1] or 0)
        wip = ledger.work_in_progress(book)
        for job in wip:
            if job["activity_id"] in (ledger.MANUFACTURING, *ledger.REACTIONS) and job["product_type_id"]:
                recipe = recipes.get(job["blueprint_type_id"])
                job["output_qty"] = job["runs"] * (recipe.product_qty if recipe else 1)
        types = sorted({t for t in tracked if held.get(t) or listed.get(t)} |
                       {j["product_type_id"] for j in wip if j.get("output_qty")})
        figures = market.book_figures(client, types, market.hub_scope("jita"))
        table = market.price_table(client)

        def price(type_id: int) -> tuple[float | None, str]:
            if type_id in figures.min_sell:
                return figures.min_sell[type_id], "jita_sell"
            ref = table.reference(type_id)
            if ref is not None and ref.average_price is not None:
                return ref.average_price, "ccp_average"
            return None, "none"

        opening = {int(r[0]): float(r[1]) for r in conn.execute("SELECT type_id, price FROM opening_prices")}
        rows = []
        for type_id in types:
            qty = held.get(type_id, 0) + listed.get(type_id, 0)
            if not qty:
                continue
            pool = book.pools.get(("item", type_id))
            unit_cost = pool.unit if pool and pool.unit is not None else opening.get(type_id)
            unit_market, basis = price(type_id)
            rows.append({"type_id": type_id, "class": "finished" if type_id in finished else "material",
                         "held": held.get(type_id, 0), "in_sell_orders": listed.get(type_id, 0),
                         "unit_cost": unit_cost,
                         "cost_basis": "ledger" if pool and pool.unit is not None else "opening",
                         "cost_value": unit_cost * qty if unit_cost is not None else None,
                         "unit_market": unit_market, "market_basis": basis,
                         "market_value": unit_market * qty if unit_market is not None else None})
        for job in wip:
            job["unit_market"] = price(job["product_type_id"])[0] if job.get("output_qty") else None
            job["expected_value"] = (job["unit_market"] * job["output_qty"]
                                     if job.get("unit_market") is not None else None)
        totals = {}
        for cls in ("material", "finished"):
            subset = [r for r in rows if r["class"] == cls]
            totals[cls] = {"cost": sum(r["cost_value"] or 0 for r in subset),
                           "market": sum(r["market_value"] or 0 for r in subset),
                           "unpriced_types": sum(1 for r in subset if r["market_value"] is None)}
        totals["wip"] = {"cost": sum(j["cost"] for j in wip),
                         "expected_value": sum(j["expected_value"] or 0 for j in wip if j.get("output_qty"))}
        names = _names(client, [r["type_id"] for r in rows] + [j["product_type_id"] for j in wip] +
                       [j["blueprint_type_id"] for j in wip])
        header["warnings"] += problems
        if args.json:
            for r in rows:
                r["type_name"] = names.get(r["type_id"])
            print(json.dumps({**header, "prices_as_of": market.iso_utc(figures.meta.last_modified),
                              "items": rows, "work_in_progress": wip, "totals": totals}, indent=2))
            return 0
        lines = _print_header("ledger inventory", header)
        for cls, title in (("finished", "finished goods (hangars + open sell orders)"), ("material", "materials")):
            subset = sorted((r for r in rows if r["class"] == cls), key=lambda r: -(r["market_value"] or 0))
            if not subset:
                continue
            lines.append("")
            lines.append(f"{title}: cost {_compact(totals[cls]['cost'])}, Jita {_compact(totals[cls]['market'])}")
            shown = subset if args.all else subset[:args.limit]
            lines.append(render.table(["type", "held", "listed", "unit cost", "unit Jita", "cost", "Jita value"], [
                [_name(names, r["type_id"]), f"{r['held']:,}", f"{r['in_sell_orders']:,}",
                 _compact(r["unit_cost"]) + ("*" if r["cost_basis"] == "opening" else ""),
                 _compact(r["unit_market"]) + ("~" if r["market_basis"] == "ccp_average" else ""),
                 _compact(r["cost_value"]), _compact(r["market_value"])] for r in shown]))
            if len(shown) < len(subset):
                lines.append(f"  ... {len(subset) - len(shown)} more (--all shows every row)")
        if wip:
            lines.append("")
            lines.append(f"work in progress: {len(wip)} jobs, cost so far {_compact(totals['wip']['cost'])}, "
                         f"manufacturing output worth {_compact(totals['wip']['expected_value'])} at Jita")
        lines.append("* unit cost at the opening price (stock older than the ledger); "
                     "~ CCP average where Jita has no sell order")
        _print_notes(lines, header)
        print("\n".join(lines))
        return 0


# ---------------------------------------------------------------------------
# notes and progress
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _body(args) -> str:
    if args.body_file:
        if args.body_file == "-":
            return sys.stdin.read()
        try:
            with open(args.body_file, encoding="utf-8") as fh:
                return fh.read()
        except OSError as err:
            raise RuntimeError(f"cannot read --body-file '{args.body_file}': {err}") from err
    return args.body or ""


def _note_line(n: dict) -> list[str]:
    status = "" if n["kind"] != "todo" else ("[ ]" if n["status"] == "open" else "[x]")
    return [str(n["id"]), n["ts"][:16].replace("T", " "), n["kind"], status, n["title"]]


def _note_block(n: dict) -> str:
    head = f"#{n['id']}  {n['ts']}  {n['kind']}"
    if n["kind"] == "todo":
        head += f"  {n['status']}" + (f" {n['closed_at']}" if n["closed_at"] else "")
    return "\n".join([head, n["title"], "", n["body"].rstrip()]).rstrip()


def cmd_note(args) -> int:
    action = args.note_action
    if action is None:
        print("usage: eve-skills ledger note {add,list,show,done} [--help]", file=sys.stderr)
        return 2
    with contextlib.closing(ledger_db.connect()) as conn:
        if action == "add":
            at = args.at or _utc_now()
            with conn:
                note_id = ledger_db.add_note(conn, at, args.kind, args.title, _body(args), source=args.source)
            row = ledger_db.note(conn, note_id)
            print(json.dumps(row, indent=2) if args.json else f"note #{note_id} stored ({args.kind}, {at})")
            return 0
        if action == "done":
            with conn:
                row = ledger_db.close_note(conn, args.id, _utc_now())
            print(json.dumps(row, indent=2) if args.json else f"to-do #{row['id']} done: {row['title']}")
            return 0
        if action == "show":
            rows = ([ledger_db.note(conn, i) for i in args.ids] if args.ids
                    else ledger_db.notes(conn, kind=args.kind, limit=args.last))
            missing = [i for i, r in zip(args.ids or [], rows) if r is None]
            if missing:
                raise RuntimeError(f"no note {', '.join(map(str, missing))}")
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                print(("\n\n" + "-" * 72 + "\n\n").join(_note_block(r) for r in rows) or "no notes")
            return 0
        rows = ledger_db.notes(conn, kind=args.kind, since=args.since, open_only=args.open, text=args.grep,
                               limit=args.limit)
        if args.json:
            print(json.dumps(rows, indent=2))
        elif rows:
            print(render.table(["id", "when (UTC)", "kind", "", "title"], [_note_line(n) for n in rows]))
        else:
            print("no notes match")
        return 0


# The figures `progress` puts in columns, in order: (heading, snapshot key, formatter).
_PROGRESS_COLUMNS = (
    ("mfg run/ready", None, lambda s: f"{s['jobs_running'].get('manufacturing', 0)}/"
                                      f"{s['jobs_ready'].get('manufacturing', 0)}"),
    ("inv run/ready", None, lambda s: f"{s['jobs_running'].get('invention', 0)}/"
                                      f"{s['jobs_ready'].get('invention', 0)}"),
    ("T2 built/sold", None, lambda s: f"{s['invention_units_built']:,.0f}/{s['invention_units_sold']:,.0f}"),
    ("inv revenue", "invention_revenue", _compact),
    ("inv gross", "invention_gross_profit", _compact),
    ("net profit", "net_profit", _compact),
    ("stock at cost", "stock_at_cost", _compact),
    ("WIP", "work_in_progress_cost", _compact),
    ("sell listed", "sell_orders_listed_isk", _compact),
    ("buy open", "buy_orders_remaining_isk", _compact),
)


def cmd_progress(args) -> int:
    with contextlib.closing(_open()) as conn:
        series = ledger_db.snapshots(conn, since=args.since)
        if not args.all:
            # One row per UTC day - its last sync - so an hourly watch does not bury the trend.
            by_day: dict[str, dict] = {}
            for snap in series:
                by_day[snap["synced_at"][:10]] = snap
            series = list(by_day.values())
        todos = ledger_db.notes(conn, kind="todo", open_only=True)
        latest = ledger_db.notes(conn, limit=args.notes)
        if args.json:
            print(json.dumps({"cutover": ledger_db.cutover(conn), "last_sync": ledger_db.last_sync(conn),
                              "snapshots": series, "open_todos": todos, "recent_notes": latest}, indent=2))
            return 0
        lines = [f"ledger progress - cutover {ledger_db.cutover(conn) or '?'}, "
                 f"last sync {ledger_db.last_sync(conn) or 'never'}"]
        age = sync_age_days(ledger_db.last_sync(conn))
        if age is not None and age > ledger_db.STALE_SYNC_DAYS:
            lines.append(f"warning: last sync {age:.0f} days ago - run: eve-skills ledger sync")
        if series:
            lines.append(render.table(["synced (UTC)"] + [h for h, _k, _f in _PROGRESS_COLUMNS], [
                [s["synced_at"][:16].replace("T", " ")] + [fmt(s) if key is None else fmt(s.get(key))
                                                           for _h, key, fmt in _PROGRESS_COLUMNS]
                for s in series]))
            lines.append(f"profit figures run from {series[-1].get('pnl_since') or 'the start'}; "
                         f"'run/ready' counts jobs still running and jobs finished but not delivered")
        else:
            lines.append("no snapshots yet - every ledger sync records one")
        lines.append("")
        lines.append(f"open to-dos ({len(todos)}):")
        lines += [f"  #{t['id']} {t['title']}" for t in todos] or ["  none"]
        lines.append("")
        lines.append("recent notes:")
        lines += ([f"  #{n['id']} {n['ts'][:16].replace('T', ' ')} {n['kind']}: {n['title']}" for n in latest]
                  or ["  none"])
        print("\n".join(lines))
        return 0


def cmd_ledger(args) -> int:
    handler = {"sync": cmd_sync, "pnl": cmd_pnl, "products": cmd_products, "invention": cmd_invention,
               "inventory": cmd_inventory, "note": cmd_note, "progress": cmd_progress}.get(args.ledger_action)
    if handler is None:
        print("usage: eve-skills ledger {sync,pnl,products,invention,inventory,note,progress} [--help]",
              file=sys.stderr)
        return 2
    return handler(args)
