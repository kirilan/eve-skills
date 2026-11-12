"""Supply runway and sales replenishment, built from the same production snapshot."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import math

from . import industry, ledger, market
from .production import DONE
from .production_store import timestamp


def moment(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def depth_cost(orders, quantity):
    """Never price missing depth at the top ask. Partial cost and unfilled quantity stay explicit."""
    remaining, cost = quantity, 0.0
    for r in sorted(orders, key=lambda r: r['price']):
        take = min(remaining, max(0, r.get('volume', r.get('volume_remain', 0))))
        cost += take * r['price']
        remaining -= take
        if remaining <= 0:
            break
    filled = quantity - remaining
    return dict(requested=quantity, filled=filled, unfilled=remaining, cost=cost,
                average=cost / filled if filled else None, complete=remaining == 0)


def quote_purchase(client, type_id, quantity, hub, *, freight=None, volume=None):
    scope = market.hub_scope(hub)
    quote, depth = market.quote(client, type_id, scope, depth=100000)
    result = depth_cost(depth['sell'], quantity)
    result.update(hub=hub, as_of=market.iso_utc(quote.meta.last_modified), expires=market.iso_utc(quote.meta.expires))
    result['freight'] = (freight.unit_cost(volume * result['filled'], result['cost']) if volume is not None else None) if freight else 0
    result['landed_cost'] = result['cost'] + result['freight'] if result['freight'] is not None else None
    return result


def historical_use(facts, book, recipes, invention, facilities, since):
    """Per-facility input use; preserve ledger's ME and decryptor assumptions."""
    used = Counter()
    for j in facts.jobs:
        if j.get('facility_id') not in facilities or j['start_date'] < since or j.get('status') in ('cancelled', 'reverted'):
            continue
        cost = book.jobs.get(j['job_id'])
        if cost is None:
            continue
        fac = j['facility_id']
        if j['activity_id'] in (1, 9):
            recipe = recipes.get(j['blueprint_type_id'])
            if recipe:
                for t, q in recipe.materials.items():
                    used[fac, t] += industry.required_quantity(q, j['runs'], cost.me or 0, cost.material_multiplier)
        elif j['activity_id'] == 8:
            inv = invention['blueprints'].get(str(j['blueprint_type_id']))
            if inv:
                for t, q in inv['m'].items():
                    used[fac, int(t)] += q * j['runs']
                if cost.decryptor_id:
                    used[fac, cost.decryptor_id] += j['runs']
    return used


def invention_pipeline(snap, recipes, invention, book):
    """Expected units, never guaranteed stock. Unknown decryptors leave output unknown."""
    rows = []
    for j in snap.jobs:
        if j['activity_id'] != 8 or j.get('status') in DONE:
            continue
        target = j.get('product_type_id')
        recipe = recipes.get(target)
        source = invention['blueprints'].get(str(j['blueprint_type_id']), {})
        out = next((p for p in source.get('p', []) if int(p[0]) == target), None)
        cost = book.jobs.get(j['job_id'])
        # An explicit no-decryptor inference is represented by a known product in book.decryptors.
        known = target in book.decryptors
        dec = invention.get('decryptors', {}).get(str(cost.decryptor_id), {}) if cost else {}
        chance = j.get('probability')
        units = (j['runs'] * chance * (int(out[1]) + dec.get('runs', 0)) * recipe.product_qty
                 if recipe and out and chance is not None and known else None)
        rows.append(dict(job_id=j['job_id'], product_id=recipe.product_id if recipe else None,
                         expected_units=units, end=j.get('end_date'), probability=chance, facility_id=j.get('facility_id'),
                         runs_per_copy=(int(out[1]) + dec.get('runs', 0)) if out and known else None,
                         basis='ESI probability and ledger-inferred decryptor' if units is not None else 'unknown decryptor or recipe'))
    return rows


def forecast(snap, usage, *, days, history_days, lead_days, facilities, plan_check=None, downstream=None):
    stock = Counter()
    for a in snap.assets:
        if a['root'] in facilities:
            stock[a['root'], a['type_id']] += max(0, a.get('quantity', 0))
    scheduled = defaultdict(list)
    now = moment(snap.now)
    horizon = now + timedelta(days=days)
    for row in (plan_check or {}).get('lines', []):
        if row['state'] in ('installed', 'changed'):
            continue
        at = max(now, moment(row.get('earliest_start') or row['start']))
        if at <= horizon:
            for t, q in row['materials'].items():
                scheduled[row['facility_id'], int(t)].append((at, q, row['line_id']))
    for key, qty in (downstream or {}).items():
        scheduled[key].append((horizon, qty, 'expected invention output (capacity capped)'))
    as_of = snap.as_of('assets')
    stale = as_of is None or any(j.get('start_date', '') > as_of for j in snap.jobs)
    result = []
    for key in sorted(set(usage) | set(scheduled)):
        fac, tid = key
        rate = usage.get(key, 0) / history_days
        events = scheduled.get(key, [])
        planned = sum(q for _, q, _ in events)
        # Explicit jobs replace, rather than add to, the historical baseline over the horizon.
        residual = max(0, rate * days - planned)
        steps = max(1, math.ceil(days))
        events = sorted(events + [(now + timedelta(days=days * n / steps), residual / steps, 'recent usage')
                                  for n in range(1, steps + 1)], key=lambda x: x[0])
        balance = stock[key]
        depleted = None
        first_block = None
        for at, qty, label in events:
            balance -= qty
            if balance < 0 and depleted is None:
                depleted, first_block = timestamp(at.isoformat()), label
        if depleted is None and rate > 0:
            depleted = timestamp((horizon + timedelta(days=max(0, balance) / rate)).isoformat())
        known = snap.complete('assets') and not stale
        buy = max(0, math.ceil(max(planned, rate * days) - stock[key])) if known else None
        deadline = timestamp((moment(depleted) - timedelta(days=lead_days)).isoformat()) if depleted else None
        bridge_demand = sum(q for at, q, _ in events if at <= now + timedelta(days=lead_days))
        result.append(dict(facility_id=fac, type_id=tid, held=stock[key] if known else None,
                           daily_use=rate, planned_need=planned, buy=buy, depletion=depleted if known else None,
                           first_blocked=first_block if known else None, order_by=deadline if known else None,
                           bid_fallback_by=deadline if known else None,
                           bridge_buy=max(0, math.ceil(bridge_demand - stock[key])) if known else None))
    return dict(generated=snap.now, days=days, history_days=history_days, lead_days=lead_days, items=result,
                documents=snap.documents, warnings=snap.warnings + (['asset balance is older than installs; refresh before buying'] if stale else []),
                assumptions=['Open buy orders are not stock. Their fallback deadline is the purchase deadline.',
                             'Demand is the greater of scheduled/expected demand and recent usage over the horizon, not their sum.',
                             'Recent use is averaged over calendar days; invention outputs are expectations.',
                             'Stock includes Deliveries and containers: move it into an installable hangar before use.'])


def replenish(snap, facts, recipes, invention, book, hub, *, days=14, history_days=30, targets=None, orders=None):
    now = moment(snap.now)
    since = timestamp((now - timedelta(days=history_days)).isoformat())
    sold = Counter()
    for t in facts.transactions:
        if not t['is_buy'] and t['date'] >= since and t.get('location_id') == hub.station_id and t.get('client_id') not in facts.internal_ids:
            sold[t['type_id']] += t['quantity']
    listed = Counter()
    for o in orders if orders is not None else facts.orders:
        if not o.get('is_buy_order', o.get('is_buy')) and o.get('location_id') == hub.station_id and o.get('state', 'open') in ('open', 'active'):
            listed[o['type_id']] += o.get('volume_remain') or 0
    held, elsewhere, wip, bpc = Counter(), Counter(), Counter(), Counter()
    originals = set()
    inventable = {int(p[0]) for source in invention['blueprints'].values() for p in source['p']}
    by_product = {r.product_id: r for r in recipes.values()}
    for a in snap.assets:
        if a['type_id'] in by_product:
            (held if a['root'] == hub.station_id else elsewhere)[a['type_id']] += max(0, a.get('quantity', 0))
    for j in snap.jobs:
        if j['activity_id'] in (1, 9) and j.get('status') not in DONE and j.get('blueprint_type_id') in recipes:
            recipe = recipes[j['blueprint_type_id']]
            wip[recipe.product_id] += j['runs'] * recipe.product_qty
    busy = {j.get('blueprint_id') for j in snap.jobs if j.get('status') not in DONE}
    for b in snap.blueprints:
        if b.get('quantity') == -1 and b['type_id'] in recipes:
            originals.add(recipes[b['type_id']].product_id)
        if b.get('quantity') == -2 and b['item_id'] not in busy and b['type_id'] in recipes:
            recipe = recipes[b['type_id']]
            bpc[recipe.product_id] += max(0, b['runs']) * recipe.product_qty
    pipeline = invention_pipeline(snap, recipes, invention, book)
    expected = Counter()
    unknown = set()
    for p in pipeline:
        if p['expected_units'] is None:
            unknown.add(p['product_id'])
        elif p.get('end') and moment(p['end']) <= now + timedelta(days=days):
            expected[p['product_id']] += p['expected_units']
    types = set(sold) | set(listed) | set(held) | set(elsewhere) | set(wip) | set(bpc) | set(expected) | (unknown - {None}) | set(targets or {})
    rows = []
    assets_at, bps_at = snap.as_of('assets'), snap.as_of('blueprints')
    stale = assets_at is None or bps_at is None or any(
        j.get('start_date', '') > min(assets_at, bps_at) or
        (j.get('status') == 'delivered' and (j.get('completed_date') or j.get('end_date') or '') > min(assets_at, bps_at))
        for j in snap.jobs)
    for t in sorted(types):
        rate = sold[t] / history_days
        target = (targets or {}).get(t, math.ceil(rate * days))
        gap = max(0, target - listed[t])
        action = ('list' if gap and held[t] else 'haul' if gap and elsewhere[t] else
                  'pause' if not gap or wip[t] + expected[t] >= gap else 'build' if bpc[t] or t in originals else
                  'invent' if t not in unknown and t in by_product and by_product[t].blueprint_id in inventable else 'inspect')
        if not rate and t not in (targets or {}):
            action = 'set trial target'
        if stale or not snap.complete('assets') or not snap.complete('jobs') or not snap.complete('blueprints') or (orders is not None and not snap.complete('orders')):
            action = 'refresh'
        row = dict(type_id=t, hub=hub.key, sold=sold[t], daily_sales=rate, demand_basis='own sales at this hub',
                         target=target, listed=listed[t], held_at_hub=held[t], held_elsewhere=elsewhere[t],
                         manufacturing=wip[t], copy_units=bpc[t], invention_expected=expected[t] if t not in unknown else None,
                         action=action, list_or_haul=min(gap, held[t] + elsewhere[t]),
                         production_gap=max(0, gap - held[t] - elsewhere[t] - wip[t] - bpc[t] - expected[t]))
        if stale or not snap.complete('assets'):
            row.update(held_at_hub=None, held_elsewhere=None, list_or_haul=None)
        if not snap.complete('jobs'):
            row.update(manufacturing=None, invention_expected=None)
        if stale or not snap.complete('blueprints'):
            row['copy_units'] = None
        if orders is not None and not snap.complete('orders'):
            row['listed'] = None
        if action in ('refresh', 'inspect') or t in unknown:
            row['production_gap'] = None
        rows.append(row)
    return dict(generated=snap.now, hub=hub.key, items=rows, documents=snap.documents, warnings=snap.warnings + (['assets/blueprints predate jobs; refresh before acting'] if stale else []),
                assumptions=['Own sales use calendar days and may understate demand during stockouts; small samples are not proven demand.',
                             'Each destination is a separate scenario. Do not allocate the same unlisted stock to two hubs.',
                             'Regional volume is not station demand. Use an explicit trial target for an untested product.',
                             'Copies and invention are pipeline capacity, not finished stock; build requires a material/slot check.'])
