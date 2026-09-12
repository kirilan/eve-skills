"""eve-skills command line interface: the argument parser, the command table and main().

Each command area lives in its own module - `cmd_market`, `cmd_build_cost`, `cmd_skills`,
`cmd_orders`, `cmd_watch` - and every subcommand this parser defines needs an entry in
HANDLERS below (or, for `skills`, the default path in main())."""

from __future__ import annotations

import argparse
import sys
import time


from . import __version__, alphadata, doctor as doctor_mod, esi as esi_mod, exports, industry, market, render, sso, watchstate
from . import cmd_build_cost, cmd_market, cmd_orders, cmd_skills, cmd_watch


def cmd_login(args):
    scopes = (["attributes"] if args.attributes else []) + [s.strip() for s in (args.scopes or "").split(",") if s.strip()]
    record = sso.login(client_id=args.client_id, client_secret=args.client_secret, port=args.port,
                       scopes=scopes, manual=args.manual)
    print(f"Logged in as {record['character_name']} ({record['character_id']}).")
    print("Run eve-skills login again (selecting a different character) to add another.")


def cmd_logout(args):
    char_id = sso.resolve_character(args.char) if args.char else None
    sso.clear_tokens(char_id)
    print(f"Removed stored tokens for {args.char}." if char_id else "Removed stored tokens for all characters.")


def cmd_chars(args):
    records = sso.list_characters()
    if not records:
        print("no characters logged in — run: eve-skills login")
        return
    rows = []
    for r in records:
        left_min = max(int((r["expires_at"] - time.time()) / 60), 0)
        rows.append([str(r["character_id"]), r.get("character_name") or "?", f"{left_min} min", "yes" if r.get("refresh_token") else "no"])
    print(render.table(["character id", "name", "access token left", "auto-refresh"], rows))


def cmd_attributes(args):
    records = [sso.resolve_character(args.char)] if args.char else [r["character_id"] for r in sso.list_characters()]
    if not records:
        raise RuntimeError("not logged in - run: eve-skills login")
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    blocks, missing = [], []
    for cid in records:
        rec = sso.get_access_token(cid)
        name = rec.get("character_name") or str(cid)
        if not sso.has_feature(rec, "attributes"):
            missing.append(name)
            continue
        a = client.get(f"/characters/{cid}/attributes", token=rec["access_token"])
        attrs = "  ".join(f"{k[:3].upper()} {a.get(k)}" for k in ("perception", "intelligence", "memory", "charisma", "willpower"))
        last_remap = (a.get("last_remap_date") or "")[:10] or "never"
        block = f"{name} (id {cid})\n  {attrs}\n  remaps available: {a.get('accumulated_remaps', '?')}   last remap: {last_remap}"
        if a.get("accelerator_bonus_days"):
            block += f"   accelerator days left: {a['accelerator_bonus_days']}"
        blocks.append(block)
    for name in missing:
        blocks.append(f"{name}: no skills consent - run: eve-skills login  (pick '{name}' in the browser)")
    print("\n\n".join(blocks))


def cmd_update_data(args):
    summary = alphadata.update(build=args.build)
    print(f"Updated to SDE build {summary['build']}:")
    for race, g in sorted(summary["grades"].items()):
        print(f"  {g['name']}: {g['skills']} alpha-trainable skills")
    print(f"  skill catalog: {summary['catalog_skills']} skills (name, rank, attributes, prerequisites)")
    print(f"  blueprint materials: {summary['blueprint_products']} products a manufacturing or reaction "
          f"blueprint builds (materials, run time, batch limit)")


def build_parser() -> argparse.ArgumentParser:
    """The whole command surface. Every subcommand here needs an entry in HANDLERS."""
    parser = argparse.ArgumentParser(
        prog="eve-skills",
        description="Show your EVE Online character's skills, training queue and alpha/omega access.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_login = sub.add_parser("login", help="authorize via EVE SSO (opens browser)")
    p_login.add_argument("--client-id", help="application client id from https://developers.eveonline.com/applications")
    p_login.add_argument("--client-secret", help="only for confidential-type app registrations")
    p_login.add_argument("--port", type=int, help="exact loopback callback port (must match the registered redirect URL; default tries 8635-8637)")
    p_login.add_argument("--manual", action="store_true", help="paste the localhost callback URL manually (for remote hosts reached over ssh)")
    p_login.add_argument("--attributes", action="store_true", help="include character attributes (already covered by the standard skills consent)")
    p_login.add_argument("--scopes", help="extra consents, comma-separated: attributes,standings,jobs,assets,location,clones,orders,corp-orders,structures,all (each re-authenticates the chosen character only)")

    p_logout = sub.add_parser("logout", help="remove stored tokens")
    p_logout.add_argument("--char", help="only this character (name or id); default removes all")

    p_skills = sub.add_parser("skills", help="show skills, queue and clone state (default)")
    p_skills.add_argument("--json", action="store_true", help="machine-readable output")
    p_skills.add_argument("--filter", choices=["all", "alpha", "omega"], default="all", help="restrict the trained-skills table")
    p_skills.add_argument("--sort", choices=["name", "level", "sp"], default="name")
    p_skills.add_argument("--trained-only", action="store_true", help="omit the training queue section")
    p_skills.add_argument("--char", help="stored character name or id (default: show every stored character)")
    p_skills.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the table (all stored characters)")
    p_skills.add_argument("--week", action="store_true", help="append SP gained over the last 7 days from local history")
    p_skills.add_argument("--watch", type=int, nargs="?", const=5, metavar="MIN", help="keep refreshing every MIN minutes (default 5), announce finished training; Ctrl-C stops")
    p_skills.add_argument("--notify", action="store_true", help="with --watch: also send notify-send desktop notifications")
    p_skills.add_argument("--full", action="store_true", help="with --watch: keep the full per-character view instead of the compact status table")
    p_skills.add_argument("--no-orders", action="store_true",
                          help="with --watch: watch training only; do not poll market orders")

    sub.add_parser("chars", help="list logged-in characters")

    sub.add_parser("summary", help="one line per character (clone state, SP, queue) plus totals")

    p_events = sub.add_parser("events", help="show recorded watch events (training and market orders)")
    p_events.add_argument("--char", help="stored character name or id; a bare numeric id also matches logged-out characters")
    p_events.add_argument("--owner", metavar="VALUE",
                          help="only order events of one owner: its exact key (char:90000001, "
                               "corp:98000001) or part of its name, case-insensitive - corporation "
                               "events carry no character id, so this is how you find them")
    p_events.add_argument("--kind", action="append", metavar="K",
                          help=f"only this event kind; repeatable: {', '.join(watchstate.EVENT_KINDS)}")
    p_events.add_argument("--limit", type=int, default=50, metavar="N", help="most recent N events (default 50)")
    p_events.add_argument("--json", action="store_true", help="machine-readable output")
    p_events.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the table")

    p_attrs = sub.add_parser("attributes", help="base attributes + remap status")
    p_attrs.add_argument("--char", help="stored character name or id (default: every stored character)")

    p_standings = sub.add_parser("standings", help="agent / NPC corp / faction standings (needs login --scopes standings)")
    p_standings.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_standings.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the tables")

    p_jobs = sub.add_parser("jobs", help="industry jobs (needs login --scopes jobs)")
    p_jobs.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_jobs.add_argument("--corp", action="store_true", help="corporation industry jobs instead of personal (needs the matching director/Account-Manager role)")
    p_jobs.add_argument("--completed", action="store_true", help="include finished and cancelled jobs")
    p_jobs.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the tables")

    p_inv = sub.add_parser("inventory", help="asset inventory with real item names, grouped by location or category and valued (needs login --scopes assets)")
    p_inv.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_inv.add_argument("--corp", action="store_true", help="corporation assets instead of personal (needs the matching director/Account-Manager role)")
    p_inv.add_argument("--by", choices=["location", "category"], default="location",
                       help="group the summary by where the items are (default) or by what they are")
    p_inv.add_argument("--items", action="store_true", help="list every item row instead of the grouped summary, most valuable first")
    p_inv.add_argument("--value-at", dest="value_at", metavar="HUB|REGION",
                       help=f"value holdings at the richest standing buy order there instead of "
                            f"ESI's published reference price; a hub reads that station only: "
                            f"{', '.join(market.HUBS)}, or a region by exact name or id")
    p_inv.add_argument("--json", action="store_true", help="machine-readable output: ids and names together, with the valuation basis")
    p_inv.add_argument("--csv", action="store_true", help="full CSV rows on stdout (always per-item); the valuation footnotes go to stderr")

    p_travel = sub.add_parser("travel", help="current location, home and jump clones with implants (needs login --scopes location / clones)")
    p_travel.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_travel.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the blocks")

    p_impl = sub.add_parser("implants", help="implants fitted in the active clone (needs login --scopes clones)")
    p_impl.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_impl.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the blocks")

    p_plan = sub.add_parser("plan", help="estimate SP and time to reach target skill levels")
    p_plan.add_argument("--char", help="stored character name or id (required when several are stored)")
    p_plan.add_argument("--rate", type=float, metavar="SPH", help="override SP/hour instead of calibrating from live training / SP history")
    p_plan.add_argument("target", nargs="+", metavar="SKILL[:LEVEL]",
                        help='e.g. "Astrogeology:5" (default target L5); missing prerequisites are added automatically')

    p_market = sub.add_parser("market", help="live order-book prices for item types (public ESI, no login)")
    p_market.add_argument("type", nargs="+", metavar="TYPE",
                          help="exact type name or numeric id, e.g. Tritanium or 34")
    p_market.add_argument("--region", action="append", metavar="NAME",
                          help='quote this region (exact name or id); repeatable: --region "The Forge"')
    p_market.add_argument("--hub", action="append", metavar="HUB",
                          help=f"quote a trade hub at station level; repeatable: {', '.join(market.HUBS)}")
    # dest is mandatory here: a bare --global would hand the handler an attribute called `global`.
    p_market.add_argument("--global", dest="global_scopes", action="store_true",
                          help="scan every region with a market and add the best prices across the cluster")
    p_market.add_argument("--history", type=int, metavar="DAYS",
                          help="also show traded volume from ESI's daily regional history (daily, one day behind)")
    p_market.add_argument("--json", action="store_true", help="machine-readable output")
    p_market.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the tables")

    p_build_cost = sub.add_parser("build-cost",
                                  help="ISK cost of manufacturing an item from its blueprint, priced "
                                       "from live ESI orders (public ESI, no login)")
    p_build_cost.add_argument("type", nargs="+", metavar="TYPE",
                              help="exact type name or numeric id of the thing to build, e.g. Hound; "
                                   "repeat for several items priced off the same order books")
    p_build_cost.add_argument("--runs", type=int, default=1, metavar="N",
                              help="installs in one job (default 1); a blueprint's own maximum runs per "
                                   "install still applies, so ask for more than it and the command says no")
    p_build_cost.add_argument("--me", type=int, default=0, metavar="N",
                              help=f"material efficiency 0..{industry.MAX_ME} (default 0) on the "
                                   f"blueprint being run only; component jobs take --component-me, and "
                                   f"a reaction ignores it entirely")
    p_build_cost.add_argument("--component-me", dest="component_me", type=int, default=None,
                              metavar="N",
                              help=f"material efficiency 0..{industry.MAX_ME} for every component job "
                                   f"(default {industry.DEFAULT_COMPONENT_ME}: component blueprints are "
                                   f"ordinarily owned BPOs researched to the cap, unlike the invented "
                                   f"copy a top blueprint often is); independent of --me, and ignored "
                                   f"by a reaction component")
    p_build_cost.add_argument("--te", type=int, default=0, metavar="N",
                              help=f"time efficiency 0..{industry.MAX_TE} (default 0); changes the job's "
                                   f"time, not its ISK")
    p_build_cost.add_argument("--hub", metavar="HUB",
                              help=f"buy every material at this trade hub's station (default jita): "
                                   f"{', '.join(market.HUBS)}")
    p_build_cost.add_argument("--region", metavar="NAME",
                              help='buy anywhere in this region (exact name or id) instead of at one '
                                   'station; needs --system too, because a region has no single cost index')
    p_build_cost.add_argument("--system", metavar="NAME",
                              help="solar system whose industry cost index bills the install (exact name "
                                   "or id); defaults to the hub's own system - it does not narrow the "
                                   "order book, so --system Amarr with the default scope means buy at "
                                   "Jita, install in Amarr")
    p_build_cost.add_argument("--build", action="append", metavar="TYPE",
                              help="force this material to be built even where buying is cheaper; repeatable")
    p_build_cost.add_argument("--buy", action="append", metavar="TYPE",
                              help="force this material to be bought even where building is cheaper; repeatable")
    p_build_cost.add_argument("--build-all", action="store_true",
                              help="build every direct material that some blueprint makes, wherever that "
                                   "leads - including surplus you did not ask for")
    p_build_cost.add_argument("--buy-all", action="store_true",
                              help="buy every quoted material instead of building it: no component jobs at all")
    p_build_cost.add_argument("--facility-tax", dest="facility_tax", type=float,
                              default=industry.NPC_STATION_TAX * 100.0, metavar="PCT",
                              help=f"installation tax as a percent of EIV (default "
                                   f"{industry.NPC_STATION_TAX * 100:g} = an NPC station; a player "
                                   f"structure bills differently and only you know its rate)")
    p_build_cost.add_argument("--material-multiplier", dest="material_multiplier", type=float, default=1.0,
                              metavar="F",
                              help="aggregate material bonus as a fraction of the blueprint's requirements "
                                   "(default 1.0 = none; 0.95 is a 5%% reduction, so quantities and cost fall)")
    p_build_cost.add_argument("--json", action="store_true",
                              help="machine-readable output, including the build option that lost")
    p_build_cost.add_argument("--csv", action="store_true",
                              help="CSV material rows on stdout; the notes go to stderr")

    p_orders = sub.add_parser("orders",
                              help="open market orders of stored characters or their corps (needs login --scopes orders)")
    p_orders.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_orders.add_argument("--corp", action="store_true",
                          help="corporation orders instead of personal ones (the character needs the Accountant or Trader role in that corp)")
    p_orders.add_argument("--closed", action="store_true",
                          help="ESI's ~90-day order history with a derived state, instead of the live book")
    p_orders.add_argument("--type", help="only this exact type name or numeric id, e.g. Tritanium or 34")
    p_orders.add_argument("--buy", action="store_true", help="only buy orders")
    p_orders.add_argument("--sell", action="store_true", help="only sell orders")
    p_orders.add_argument("--limit", type=int, metavar="N",
                          help="newest N rows only; the ISK totals still cover every matching order")
    p_orders.add_argument("--json", action="store_true", help="machine-readable output")
    p_orders.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the table")
    p_orders.add_argument("--watch", type=int, nargs="?", const=5, metavar="MIN",
                          help="keep refreshing every MIN minutes (default 5), announcing filled/expired/cancelled orders; Ctrl-C stops")
    p_orders.add_argument("--notify", action="store_true",
                          help="with --watch: also send notify-send desktop notifications")

    p_extract = sub.add_parser("extract", help="Skill Extractor math for one character")
    p_extract.add_argument("--char", help="stored character name or id (required when several are stored)")

    p_doctor = sub.add_parser("doctor", help="diagnose installation, stored logins and data freshness (never writes)")
    p_doctor.add_argument("--json", action="store_true", help="machine-readable report")
    p_doctor.add_argument("--network", action="store_true",
                          help="also probe EVE SSO discovery and public ESI endpoints (unauthenticated, bounded)")
    p_doctor.add_argument("--timeout", type=float, default=doctor_mod.NET_TIMEOUT, metavar="SECONDS",
                          help=f"per-request network timeout for --network probes (default {doctor_mod.NET_TIMEOUT:g})")

    p_update = sub.add_parser("update-data", help="refresh alpha caps, the skill catalog and blueprint materials from the official SDE (~100 MB download)")
    p_update.add_argument("--build", type=int, help="specific SDE build number (default: latest)")

    return parser


HANDLERS = {"login": cmd_login, "logout": cmd_logout, "chars": cmd_chars,
            "summary": cmd_skills.cmd_summary, "attributes": cmd_attributes,
            "plan": cmd_skills.cmd_plan, "extract": cmd_skills.cmd_extract,
            "update-data": cmd_update_data, "standings": exports.cmd_standings,
            "jobs": exports.cmd_jobs, "inventory": exports.cmd_inventory,
            "travel": exports.cmd_travel, "implants": exports.cmd_implants,
            "doctor": doctor_mod.cmd_doctor, "events": cmd_watch.cmd_events,
            "market": cmd_market.cmd_market, "build-cost": cmd_build_cost.cmd_build_cost,
            "orders": cmd_orders.cmd_orders}


def _use_utf8_streams() -> None:
    """Make our own output encoding-independent, whatever the console was configured with.

    Everything this tool writes is UTF-8: the token store, the event history, ESI's JSON and the
    ``--json`` reports that round-trip it. A character name is whatever the player typed and EVE
    accounts are global, so one legitimately contains CJK or Cyrillic - and a Windows stdout that
    is not a console (piped to another program, redirected to a file, captured by CI) encodes with
    the ANSI code page, cp1252 on an English install, where those characters do not exist. The
    command then did all its work and failed on the printing: ``UnicodeEncodeError`` and exit 1 for
    a report that was fine. An interactive Windows console already speaks UTF-8 (PEP 528), so this
    changes nothing for a person at a keyboard; it only stops the redirect from corrupting the run.

    Streams with no ``reconfigure`` - a test's ``StringIO``, an already-replaced stdout - are left
    exactly as they were found.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):     # closed, detached, or not a text stream after all
            pass


def main(argv=None):
    _use_utf8_streams()      # before parsing: an argparse usage error is output too
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None or args.command == "skills":
        if args.command is None:
            args = parser.parse_args(["skills"] + (argv or []))
        try:
            return cmd_skills.cmd_skills(args) or 0
        except (RuntimeError, esi_mod.EsiError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 1

    try:
        # Handlers return None for the ordinary success path; doctor reports its own exit code.
        return int(HANDLERS[args.command](args) or 0)
    except (RuntimeError, esi_mod.EsiError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
