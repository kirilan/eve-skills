"""Production CLI: saved rounds, supply runway, replenishment, scans and dated facility evidence."""
from __future__ import annotations

import contextlib
from dataclasses import asdict, replace
from datetime import timedelta
import json
import math
from collections import Counter
import sys

from . import (alphadata, cmd_ledger, esi, industry, ledger, ledger_db, ledger_query, market,
               operation, production, production_data, production_reports as reports,
               production_scan, production_store as store, render, sso, universe)


def add_parser(sub):
    p = sub.add_parser('production', help='saved rounds, shared pools, supplies, replenishment and product scans')
    actions = p.add_subparsers(dest='production_action', required=True)
    round_p = actions.add_parser('round', help='save a round and reconcile it with ESI jobs')
    rounds = round_p.add_subparsers(dest='round_action', required=True)
    save = rounds.add_parser('save', help='save a JSON plan, resolving product and character names')
    save.add_argument('file', help='JSON file, or - for stdin')
    save.add_argument('--site')
    save.add_argument('--json', action='store_true')
    for action in ('check', 'show', 'close', 'handoff'):
        r = rounds.add_parser(action)
        r.add_argument('id', nargs='?', type=int, help='round id; default active round at --site')
        r.add_argument('--site')
        r.add_argument('--json', action='store_true')
        if action == 'handoff':
            r.add_argument('--holder', required=True)
            r.add_argument('--facility', required=True, help='configured facility key')
            r.add_argument('--at', help='ISO timestamp with timezone; default now')
            r.add_argument('--hangar', choices=['Hangar', 'Deliveries'], default='Deliveries')
            r.add_argument('--note', default='user confirmed the whole-stack transfer')
    r = rounds.add_parser('list')
    r.add_argument('--site')
    r.add_argument('--json', action='store_true')
    for action in ('supply', 'replenish', 'scan'):
        r = actions.add_parser(action)
        r.add_argument('--site')
        r.add_argument('--json', action='store_true')
        r.add_argument('--hub', help='destination/local hub; default site hub, then jita')
        if action == 'supply':
            r.add_argument('--days', type=float, default=14)
            r.add_argument('--history-days', type=float, default=7)
            r.add_argument('--lead-days', type=float, default=2)
            r.add_argument('--input-hub', default='jita')
            r.add_argument('--no-prices', action='store_true')
            r.add_argument('--round', type=int)
        elif action == 'replenish':
            r.add_argument('--days', type=float, default=14)
            r.add_argument('--history-days', type=float, default=30)
            r.add_argument('--target', action='append', default=[], metavar='ITEM=QTY', help='explicit hub stock target, including a trial line')
        else:
            r.add_argument('products', nargs='+', help='product names/IDs, or - for one per stdin line')
            r.add_argument('--input-hub', default='jita')
            r.add_argument('--seller')
            r.add_argument('--broker-pct', type=float, help='known structure listing fee percent; unknown stays unknown')
            r.add_argument('--regional-share', type=float, default=.01, help='sales scenario fraction, not station demand (default .01)')
            r.add_argument('--factory-slots', type=int, default=1)
            r.add_argument('--lab-slots', type=int, default=1)
            r.add_argument('--copy-cost', type=float, default=0, help='copy budget per invention attempt (or manufacturing copy), explicitly excluded at zero')
    f = actions.add_parser('facility', help='append immutable dated facility observations; preview accounting impact')
    f.add_argument('facility_action', choices=['list', 'record', 'impact'])
    f.add_argument('--facility', help='configured facility key (record)')
    f.add_argument('--effective', help='ISO date/time with timezone; default now (never backdated automatically)')
    f.add_argument('--reason', help='evidence for these settings at this date')
    f.add_argument('--apply', action='store_true', help='record after showing the replay impact; without this only preview')
    f.add_argument('--json', action='store_true')


def _context(args):
    op = operation.load()
    site = op.site(getattr(args, 'site', None))
    if site is None:
        raise RuntimeError('configure an operation site first: operation template / operation show')
    client = esi.Esi(esi.default_user_agent(sso.load_config()))
    return op, site, client


def _character(spec):
    cid = sso.resolve_character(str(spec))
    rec = next((r for r in sso.list_characters() if int(r['character_id']) == cid), {})
    return cid, rec.get('character_name') or str(cid)


def _read_ledger():
    try:
        conn = ledger_db.connect(readonly=True)
        if conn.execute('PRAGMA user_version').fetchone()[0] < ledger_db.SCHEMA_VERSION:
            conn.close()
            raise RuntimeError('ledger schema needs upgrading; run ledger sync')
        return conn
    except FileNotFoundError:
        raise RuntimeError('no ledger yet; run ledger sync') from None


def _facts(conn, now):
    recipes, invention = cmd_ledger._reference_data()
    facts = ledger.load(conn)
    book = ledger.replay(facts, recipes, invention, now, cmd_ledger.planetary_reference())
    return facts, book, recipes, invention


def _decorate(doc, client=None):
    names = ledger_query._type_names()
    for key in ('items', 'pool'):
        for r in doc.get(key, []):
            if 'type_id' in r:
                r['type'] = names.get(r['type_id'], f'type {r["type_id"]}')
    return doc


def _print(doc, machine):
    if machine:
        print(json.dumps(doc, indent=2, allow_nan=False))
        return
    if isinstance(doc, list):
        print(render.table(['id', 'site', 'title', 'created', 'closed'],
                           [[str(r['id']), r['site'], r['title'], r['created_at'], r.get('closed_at') or '-'] for r in doc]))
        return
    if 'round_id' in doc and 'lines' in doc:
        print(f'Round {doc["round_id"]}: {doc["title"]} ({doc["site"]}), as of {doc["generated"]}')
        print(render.table(['#', 'installer', 'activity / product', 'runs', 'state', 'finish if started', 'blockers'],
                           [[str(r['line_id']), r['installer'], r['activity'] + ': ' + r['product'], str(r['runs']),
                             r['state'], r.get('estimated_end') or (r.get('observed_job') or {}).get('end_date') or '?',
                             ('; '.join(r.get('blockers', [])) if r['state'] != 'changed' else f'observed {(r.get("observed_job") or {}).get("runs")} runs')] for r in doc['lines']]))
        for facility, sequence in doc['installer_sequence'].items():
            print(f'Whole stack at {facility}: ' + ' -> '.join(sequence))
        for h in doc['holders']:
            print(f'Pool at {h["facility_id"]}: {h["bulk_holder"]}; observed holdings {h["holders"]}; copies {h["blueprint_holders"]}')
            if h.get('confirmed_handoff'):
                print('  Latest confirmed handoff: ' + json.dumps(h['confirmed_handoff']))
        print(render.table(['facility', 'input', 'need', 'held', 'usable now', 'move to hangar', 'short'],
                           [[str(r.get(k) if r.get(k) is not None else '?') for k in
                             ('facility_id', 'type', 'need', 'held', 'usable', 'move_to_hangar', 'short')] for r in doc['pool']]))
    elif 'items' in doc:
        rows = doc['items']
        if 'lead_days' in doc:
            cols = ('type', 'facility_id', 'held', 'daily_use', 'buy', 'bridge_buy', 'first_blocked', 'depletion', 'order_by', 'open_bid_units')
        elif rows and 'action' in rows[0]:
            cols = ('type', 'listed', 'held_at_hub', 'held_elsewhere', 'manufacturing', 'copy_units', 'invention_expected', 'action', 'list_or_haul')
        else:
            cols = ('product', 'status', 'decryptor', 'cost_per_unit', 'margin', 'profit_per_factory_hour', 'bottleneck', 'daily_profit')
        print(render.table(list(cols), [[str(r.get(k) if r.get(k) is not None else '?') for k in cols] for r in rows]))
        for r in rows:
            for key in ('local_purchase', 'hub_purchase', 'bridge_purchase'):
                if key in r:
                    q = r[key]
                    landed = f'{q["landed_cost"]:,.0f}' if q['landed_cost'] is not None else '?'
                    print(f'{r.get("type")}: {key} {q["filled"]}/{q["requested"]}, landed {landed} ISK; book {q["as_of"]}')
    else:
        print(json.dumps(doc, indent=2, allow_nan=False))
    for d in doc.get('documents', []):
        if d['section'] in ('jobs', 'assets', 'blueprints', 'orders'):
            print(f'{d["owner"]} {d["section"]}: as of {d["as_of"] or "unknown"}; next refresh {d["expires"] or "unknown"}')
    for warning in doc.get('warnings', []):
        print('warning: ' + warning)
    if doc.get('fee_quality'):
        print('Fee completeness: ' + doc['fee_quality']['status'] + '. ' + doc['fee_quality']['explanation'])
    if doc.get('assumptions'):
        print('Assumptions:')
        for assumption in doc['assumptions']:
            print('  ' + assumption)


def _round(args):
    op, site, client = _context(args)
    action = args.round_action
    if action == 'save':
        try:
            if args.file == '-':
                doc = json.load(sys.stdin)
            else:
                with open(args.file, encoding='utf-8') as fh:
                    doc = json.load(fh)
        except (OSError, ValueError) as err:
            raise RuntimeError(f'cannot read round: {err}') from err
        index = industry.recipe_index(alphadata.blueprint_materials())
        plan = production.normalize(doc, op, site, index, alphadata.blueprint_invention(),
                                    lambda spec: market.resolve_type(client, str(spec)), _character, store.timestamp())
        allowed = {_character(n)[0] for n in (site.characters or site.readers)}
        if site.owner == 'personal' and any(r['installer_id'] not in allowed for r in plan['lines']):
            raise RuntimeError('every installer must be a member of the selected site')
        with contextlib.closing(ledger_db.connect()) as conn:
            ident = store.save_round(conn, plan)
        _print(dict(id=ident, **plan), args.json)
        return
    write = action in ('close', 'handoff')
    with contextlib.closing(ledger_db.connect() if write else _read_ledger()) as conn:
        if action == 'list':
            doc = [dict(r) for r in conn.execute('SELECT id,site,title,created_at,closed_at FROM production_rounds WHERE site=? ORDER BY id DESC', (site.name,))]
        else:
            plan = store.get_round(conn, getattr(args, 'id', None), site.name)
            if plan['site'] != site.name:
                site = op.site(plan['site'])
            if action == 'close':
                with conn:
                    conn.execute('UPDATE production_rounds SET closed_at=? WHERE id=? AND closed_at IS NULL', (store.timestamp(), plan['id']))
                doc = store.get_round(conn, plan['id'])
            elif action == 'handoff':
                if plan['closed_at']:
                    raise RuntimeError('cannot hand off a closed round')
                fac = op.facility(args.facility)
                holder, _ = _character(args.holder)
                if args.facility not in site.facility_keys() or holder not in {_character(c)[0] for c in site.characters}:
                    raise RuntimeError('handoff facility and holder must belong to this personal site')
                at = store.timestamp(args.at)
                if at > store.timestamp():
                    raise RuntimeError('a confirmed transfer cannot be in the future')
                with conn:
                    conn.execute('INSERT INTO pool_handoffs(round_id,facility_id,holder_id,at,hangar,note) VALUES(?,?,?,?,?,?)',
                                 (plan['id'], fac.location_id, holder, at, args.hangar, args.note))
                doc = dict(round_id=plan['id'], confirmed_holder=holder, at=at, evidence='user confirmation; ESI may lag')
            elif action == 'show':
                doc = plan
            else:
                snap = production_data.read_site(client, site, extra_characters=tuple(r['installer'] for r in plan['lines']))
                handoffs = [dict(r) for r in conn.execute('SELECT * FROM pool_handoffs WHERE round_id=? ORDER BY at,id', (plan['id'],))]
                doc = production.check_round(plan, snap, handoffs)
                _decorate(doc)
    _print(doc, args.json)


def _facility(args):
    now = store.timestamp()
    if args.facility_action == 'list':
        with contextlib.closing(_read_ledger()) as conn:
            doc = dict(observations=store.histories(conn))
        _print(doc, args.json)
        return
    with contextlib.closing(_read_ledger()) as conn:
        facts, book, recipes, invention = _facts(conn, now)
    if args.facility_action == 'record':
        if not args.facility or not args.reason:
            raise RuntimeError('record needs --facility and --reason; --effective defaults to now')
        fac = operation.load().facility(args.facility)
        if fac.location_id is None:
            raise RuntimeError('facility needs a location_id')
        effective = store.timestamp(args.effective or now)
        if any(r['facility_id'] == fac.location_id and r['effective_at'] == effective for r in facts.facility_history):
            raise RuntimeError('an immutable observation already exists at that effective time')
        observation = dict(id=-1, facility_id=fac.location_id, effective_at=effective, recorded_at=now,
                           reason=args.reason, profile=asdict(fac))
        proposed = replace(facts, facility_history=sorted([*facts.facility_history, observation], key=lambda r: r['effective_at']))
        after = ledger.replay(proposed, recipes, invention, now, cmd_ledger.planetary_reference())
        before = book
    else:
        before = ledger.replay(replace(facts, facility_history=[]), recipes, invention, now, cmd_ledger.planetary_reference())
        after = book
    changed = [dict(job_id=jid, before=old.total, after=after.jobs[jid].total,
                    delta=after.jobs[jid].total - old.total,
                    observation=after.jobs[jid].facility_observation_id)
               for jid, old in before.jobs.items() if abs(old.total - after.jobs[jid].total) > .005]
    doc = dict(changed_jobs=changed, job_cost_delta=sum(r['delta'] for r in changed),
               stock_cost_delta=sum(p.cost for p in after.pools.values()) - sum(p.cost for p in before.pools.values()),
               pnl_before=ledger.pnl(before), pnl_after=ledger.pnl(after), fee_quality=ledger.fee_quality(after),
               applied=False, assumptions=['Weighted average, opening stock and invention pooling rules are unchanged.',
                                          'Job-cost delta is not net-profit delta; inventory and later sales also change.'])
    if args.facility_action == 'record':
        doc['observation'] = observation
        if args.apply:
            with contextlib.closing(ledger_db.connect()) as conn:
                store.record_facility(conn, fac.location_id, effective, asdict(fac), args.reason, now)
            doc['applied'] = True
    _print(doc, args.json)


def _pipeline_materials(snap, book, recipes, invention, op, site, days, plan_check=None):
    """Expected downstream builds, after explicit builds, capped by free measured factory capacity."""
    from .cmd_restock import build_requirements
    fac = op.facility(site.facility_for('manufacturing'))
    labs = op.facility(site.facility_for('invention'))
    pipeline = [r for r in reports.invention_pipeline(snap, recipes, invention, book)
                if r['facility_id'] == labs.location_id]
    remaining = [r for r in (plan_check or {}).get('lines', []) if r['state'] not in ('installed', 'changed')]
    for row in remaining:
        if row['activity_id'] == 8:
            pipeline.append(dict(line_id=row['line_id'], product_id=row['product_id'],
                                 expected_units=row['expected_units'], end=row.get('estimated_end'),
                                 runs_per_copy=None, basis='saved invention probability; install prerequisites apply'))
    builders = {sso.resolve_character(n) for n in site.characters or site.readers}
    budget = sum((production_data.free_slots(snap, cid, 1) or 0) for cid in builders) * 24 * days
    planned_units = Counter()
    for row in remaining:
        if row['activity_id'] == 1:
            planned_units[row['product_id']] += row['runs'] * recipes[row['blueprint_type_id']].product_qty
            h = production_data.measured_hours(snap.jobs, row['installer_id'], 1, row['blueprint_type_id'], fac.location_id)
            h = h or row.get('hours_per_run')
            # Without a duration the spare capacity cannot be proven.
            budget = max(0, budget - row['runs'] * h) if h else 0
    needed = Counter()
    for row in sorted(pipeline, key=lambda r: r.get('end') or ''):
        recipe = next((r for r in recipes.values() if r.product_id == row['product_id']), None)
        row['forecast_units'] = None
        if recipe is None or row['expected_units'] is None or not row.get('end'):
            continue
        wait_days = max(0, (reports.moment(row['end']) - reports.moment(snap.now)).total_seconds() / 86400)
        available_days = max(0, days - wait_days)
        hours = [production_data.measured_hours(snap.jobs, cid, 1, recipe.blueprint_id, fac.location_id) for cid in builders]
        hours = [h for h in hours if h]
        if not hours:
            continue
        h = max(hours)  # conservative throughput among measured builders
        covered = min(row['expected_units'], planned_units[row['product_id']])
        planned_units[row['product_id']] -= covered
        free = sum((production_data.free_slots(snap, cid, 1) or 0) for cid in builders)
        runs = min(math.ceil((row['expected_units'] - covered) / recipe.product_qty),
                   int(min(budget, free * 24 * available_days) / h))
        budget -= runs * h
        row['forecast_units'] = runs * recipe.product_qty
        row['already_planned_units'] = covered
        inputs = build_requirements(recipe, runs * recipe.product_qty, 0,
                                    fac.material_multiplier('manufacturing'), job_runs=row.get('runs_per_copy') or 1)
        for t, q in inputs.items():
            needed[fac.location_id, t] += q
    return needed, pipeline


def cmd_production(args):
    if args.production_action == 'round':
        return _round(args)
    if args.production_action == 'facility':
        return _facility(args)
    op, site, client = _context(args)
    for name in ('days', 'history_days', 'factory_slots', 'lab_slots'):
        if hasattr(args, name):
            production.positive(getattr(args, name), '--' + name.replace('_', '-'))
    if hasattr(args, 'lead_days'):
        production.positive(args.lead_days, '--lead-days', zero=True)
    hub = args.hub or site.hub or 'jita'
    snap = production_data.read_site(client, site, market_characters=(site.seller,) if site.seller else (),
                                     with_orders=args.production_action in ('replenish', 'scan', 'supply'))
    if args.production_action == 'scan':
        if not 0 < args.regional_share <= 1 or not math.isfinite(args.regional_share):
            raise RuntimeError('--regional-share must be in (0,1]')
        production.positive(args.copy_cost, '--copy-cost', zero=True)
        if args.broker_pct is not None and (not math.isfinite(args.broker_pct) or not 0 <= args.broker_pct <= 100):
            raise RuntimeError('--broker-pct must be 0..100')
        specs = [x.strip() for x in sys.stdin if x.strip()] if args.products == ['-'] else args.products
        if len(specs) > 1000:
            raise RuntimeError('scan at most 1000 products per invocation')
        products = list(dict.fromkeys(market.resolve_type(client, s) for s in specs))
        with contextlib.closing(_read_ledger()) as conn:
            facts, book, recipes, invention = _facts(conn, snap.now)
            since = store.timestamp((reports.moment(snap.now) - timedelta(days=30)).isoformat())
            own_sales = Counter()
            for tx in facts.transactions:
                if not tx['is_buy'] and tx['date'] >= since and tx.get('location_id') == market.get_hub(hub).station_id and tx.get('client_id') not in facts.internal_ids:
                    own_sales[tx['type_id']] += tx['quantity']
            realized = {r['type_id']: r for r in ledger.products(book) if r['scope'] in (ledger.SCOPE_INVENTION, ledger.SCOPE_OTHER)}
            last_sync = ledger_db.last_sync(conn)
        doc = production_scan.scan(client, op, site, snap, industry.recipe_index(alphadata.blueprint_materials()),
                                   alphadata.blueprint_invention(), products, hub=hub, input_hub=args.input_hub,
                                   seller_name=args.seller or site.seller, broker_pct=args.broker_pct,
                                   regional_share=args.regional_share, factory_slots=args.factory_slots,
                                   lab_slots=args.lab_slots, copy_cost=args.copy_cost,
                                   own_daily={t: q / 30 for t, q in own_sales.items()}, realized=realized)
        doc['fee_quality'] = ledger.fee_quality(book)
        doc['last_ledger_sync'] = last_sync
        doc['warnings'].append('own sales and realized costs come from the ledger; sync before comparing projections')
    else:
        with contextlib.closing(_read_ledger()) as conn:
            facts, book, recipes, invention = _facts(conn, snap.now)
            if args.production_action == 'replenish':
                targets = {}
                for spec in args.target:
                    name, sep, qty = spec.rpartition('=')
                    if not sep:
                        raise RuntimeError('--target expects ITEM=QTY')
                    try:
                        quantity = int(qty)
                    except ValueError:
                        raise RuntimeError('--target quantity must be a nonnegative integer') from None
                    production.positive(quantity, '--target', integer=True, zero=True)
                    targets[market.resolve_type(client, name)[0]] = quantity
                doc = reports.replenish(snap, facts, recipes, invention, book, market.get_hub(hub),
                                        days=args.days, history_days=args.history_days, targets=targets, orders=snap.orders)
                doc['last_ledger_sync'] = ledger_db.last_sync(conn)
                doc['warnings'].append('sales are from the ledger; run ledger sync before replenishment; open orders are read live')
            else:
                plan_check = None
                active = conn.execute('SELECT id FROM production_rounds WHERE site=? AND closed_at IS NULL', (site.name,)).fetchone()
                if args.round or active:
                    plan = store.get_round(conn, args.round or active['id'])
                    if plan['site'] != site.name:
                        raise RuntimeError('--round belongs to another site')
                    plan_check = production.check_round(plan, snap)
                facilities = {op.facility(k).location_id for k in site.facility_keys()}
                since = store.timestamp((reports.moment(snap.now) - timedelta(days=args.history_days)).isoformat())
                usage = reports.historical_use(facts, book, recipes, invention, facilities, since)
                downstream, pipeline = _pipeline_materials(snap, book, recipes, invention, op, site, args.days, plan_check)
                doc = reports.forecast(snap, usage, days=args.days, history_days=args.history_days,
                                       lead_days=args.lead_days, facilities=facilities, plan_check=plan_check, downstream=downstream)
                doc['invention_pipeline'] = pipeline
                doc['last_ledger_sync'] = ledger_db.last_sync(conn)
                doc['assumptions'].append('Expected invention builds use ME0 and currently free, measured factory capacity; unknown timings are excluded.')
                for row in doc['items']:
                    row['open_bid_units'] = sum((o.get('volume_remain') or 0) for o in snap.orders
                                                if o.get('is_buy_order', o.get('is_buy')) and o.get('state') in ('open', 'active') and o['type_id'] == row['type_id'])
                    row['open_bid_units'] = row['open_bid_units'] if snap.complete('orders') else None
                    if not args.no_prices and row['buy']:
                        tid = row['type_id']
                        info = universe.type_info(client, [tid]).get(tid)
                        volume = info.packaged_volume if info else None
                        route = op.freight.get(site.freight_in) if site.freight_in else None
                        purchases = [('local_purchase', row['buy'], hub, None),
                                     ('hub_purchase', row['buy'], args.input_hub, route)]
                        if row['bridge_buy']:
                            purchases.append(('bridge_purchase', row['bridge_buy'], hub, None))
                        for key, quantity, source, freight in purchases:
                            try:
                                row[key] = reports.quote_purchase(client, tid, quantity, source, freight=freight, volume=volume)
                            except (esi.EsiError, RuntimeError) as err:
                                doc['warnings'].append(f'{key} {tid}: {err}; no substitute quote')
                if not args.no_prices:
                    doc['assumptions'].append('Freight shown per item excludes a route minimum; apply the contract minimum to the combined haul.')
            doc['fee_quality'] = ledger.fee_quality(book)
            doc['assumptions'] += [cmd_ledger.NOTES.get(k, k + ': {n}').format(n=v) for k, v in book.notes.items()]
    _decorate(doc)
    _print(doc, args.json)
