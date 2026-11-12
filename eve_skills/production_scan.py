"""Comparable candidate economics using industry.plan_build and the live site configuration."""
from __future__ import annotations

from dataclasses import replace
import statistics

from . import cmd_market, industry, market, pricing, sso
from .production_data import measured_hours


def eligibility(requirements, levels):
    if requirements is None or levels is None:
        return None
    return all(levels.get(int(k), 0) >= v for k, v in requirements.items())


def invention_quote(recipe, source, output, decryptor, prices, facility, *, probability=None, copy_cost=0):
    """Expected cost per manufactured unit; failed attempts are included through the probability.

    Invention EIV is 2% of the output blueprint's unresearched input value (CCP, EVE Industry):
    https://www.eveonline.com/ja/news/view/eve-industry-all-you-want-to-know
    The installation charge uses the same industry.job_cost convention as manufacturing.
    Copy acquisition is reported separately, never silently assumed purchased for free.
    """
    dec = decryptor or {}
    chance = probability if probability is not None else float(output[2]) * dec.get('probability', 1)
    chance = min(1.0, chance)
    runs = int(output[1]) + dec.get('runs', 0)
    wanted = {int(t): q for t, q in source['m'].items()}
    if dec.get('type_id'):
        wanted[dec['type_id']] = 1
    missing = [t for t in wanted if t not in prices.unit]
    eiv, eiv_missing = industry.estimated_item_value(recipe, 1, prices.adjusted)
    fee = industry.job_cost(eiv * .02, facility)
    attempt = sum(q * prices.unit[t] for t, q in wanted.items() if t in prices.unit) + fee + copy_cost
    cost = attempt / (chance * runs * recipe.product_qty) if chance > 0 and runs > 0 and not missing and not eiv_missing else None
    return dict(cost_per_unit=cost, fee_per_attempt=fee, chance=chance, runs=runs,
                me=2 + dec.get('me', 0), te=4 + dec.get('te', 0),
                missing=missing, eiv_missing=list(eiv_missing))


def scan(client, op, site, snap, index, invention, products, *, hub, input_hub, seller_name,
         broker_pct=None, regional_share=.01, factory_slots=1, lab_slots=1, copy_cost=0, own_daily=None, realized=None):
    build_fac = op.facility(site.facility_for('manufacturing'))
    inv_fac = op.facility(site.facility_for('invention'))
    if not build_fac.system:
        raise RuntimeError('scan needs the manufacturing system in operation.json')
    indices = industry.cost_indices(client)
    def facility(f, activity):
        if not f.system:
            raise RuntimeError(f'scan needs the {activity} system in operation.json')
        system = market.resolve_system(client, f.system)[0]
        cost_index = indices.index(system, activity)
        if cost_index is None:
            raise RuntimeError(f'no {activity} cost index for {f.system}')
        return industry.Facility(cost_index, f.tax if f.tax is not None else industry.NPC_STATION_TAX,
                                 f.material_multiplier(activity), time_multiplier=f.time_multiplier(activity))
    factory = facility(build_fac, 'manufacturing')
    sources = {int(p[0]): (int(k), v, p) for k, v in invention['blueprints'].items() for p in v['p']}
    labs = (facility(inv_fac, 'invention') if any(t in index and index[t].blueprint_id in sources for t, _ in products) else None)
    wanted = set()
    for tid, _ in products:
        if tid not in index:
            continue
        r = index[tid]
        wanted |= set(r.materials) | {tid}
        if r.blueprint_id in sources:
            wanted |= {int(t) for t in sources[r.blueprint_id][1]['m']}
    wanted |= {int(d) for d in invention['decryptors']}
    figures = market.book_figures(client, wanted, market.hub_scope(input_hub))
    refs = market.price_table(client)
    adjusted = {t: r.adjusted_price for t in wanted if (r := refs.reference(t)) and r.adjusted_price is not None}
    prices = industry.Prices(unit=figures.min_sell, adjusted=adjusted)
    seller, seller_note = cmd_market.resolve_seller(client, seller_name)
    scope = market.hub_scope(hub)
    place = cmd_market.listing_places(client, seller, [scope.location_id]).get(scope.location_id)
    fee = broker_pct if broker_pct is not None else place.fee_pct if place else None
    warnings = ([seller_note] if seller_note else []) + list(snap.warnings)
    if fee is None:
        warnings.append('structure broker fee unknown; pass --broker-pct to calculate net profit')
    rows = []
    names = {cid: name for cid, name in snap.characters.items()}
    members = {sso.resolve_character(n) for n in site.characters or site.readers}
    levels_by_member = {cid: snap.skills.get(cid) for cid in members}
    for tid, name in products:
        recipe = index.get(tid)
        if recipe is None or recipe.activity != 'manufacturing':
            rows.append(dict(type_id=tid, product=name, status='no manufacturing recipe'))
            continue
        builders = {cid: eligibility(recipe.skills, levels) for cid, levels in levels_by_member.items()}
        quotes = []
        source = sources.get(recipe.blueprint_id)
        decs = [None] + [dict(d, type_id=int(k)) for k, d in invention['decryptors'].items()] if source else [None]
        for dec in decs:
            inv = invention_quote(recipe, source[1], source[2], dec, prices, labs, copy_cost=copy_cost) if source else None
            me, te, runs = (inv['me'], inv['te'], inv['runs']) if inv else (0, 0, 1)
            if runs < 1 or me < 0 or te < 0:
                continue
            sized = replace(recipe, max_runs=max(recipe.max_runs, runs))
            # Buy direct components. Their factory hours and stock are not available for recursive builds.
            plan = industry.plan_build(sized, index, prices, factory, runs=runs, me=me, te=te,
                                       force={t: 'buy' for t in recipe.materials if t in prices.unit})
            absent = [t for t in recipe.materials if t not in prices.unit]
            if absent or plan.eiv_missing or plan.cost_per_unit is None or (inv and inv['cost_per_unit'] is None):
                continue
            cost = plan.cost_per_unit + (inv['cost_per_unit'] if inv else 0) + (copy_cost / plan.units if not inv else 0)
            hours = []
            for cid, can in builders.items():
                if can is True:
                    h = measured_hours(snap.jobs, cid, 1, recipe.blueprint_id, build_fac.location_id)
                    if h:
                        hours.append(h / recipe.product_qty)
            factory_h = statistics.median(hours) if hours else plan.time / 3600 / plan.units
            inventors = ({cid: eligibility(source[1].get('s'), levels) for cid, levels in levels_by_member.items()}
                         if source else {})
            inv_times = [measured_hours(snap.jobs, cid, 8, source[0], inv_fac.location_id)
                         for cid, can in inventors.items() if can is True] if source else []
            inv_times = [h for h in inv_times if h]
            lab_h = ((statistics.median(inv_times) if inv_times else source[1]['t'] / 3600 * labs.time_multiplier)
                     / (inv['chance'] * runs * recipe.product_qty)) if inv and inv['chance'] > 0 else 0
            quotes.append(dict(decryptor=dec['name'] if dec else 'none', cost_per_unit=cost,
                               manufacturing_cost=plan.cost_per_unit, invention_cost=inv['cost_per_unit'] if inv else 0,
                               probability=inv['chance'] if inv else None, factory_hours=factory_h, lab_hours=lab_h,
                               timing='measured' if hours else 'base TE and facility; no character speed bonus',
                               inventors={names.get(c, str(c)): v for c, v in inventors.items()}))
        if not quotes:
            rows.append(dict(type_id=tid, product=name, status='incomplete input prices or EIV', margin=None))
            continue
        quote, depth = market.quote(client, tid, scope, depth=100000)
        hist = market.history_stats(client, scope.region_id, tid, 30)
        listed, _ = pricing.sell_price([r for r in depth['sell'] if r['order_id'] not in {o['order_id'] for o in snap.orders}], hist.volume_per_day if hist else 0, 1)
        conservative = min(listed, hist.average_price) if listed is not None and hist and hist.average_price else None
        net = market.net_price(conservative, seller.sales_tax_pct + fee) if fee is not None else None
        for q in quotes:
            q['margin'] = net - q['cost_per_unit'] if net is not None else None
            factory_capacity = factory_slots * 24 / q['factory_hours'] if q['factory_hours'] else 0
            lab_capacity = lab_slots * 24 / q['lab_hours'] if q['lab_hours'] else float('inf')
            own_rate = (own_daily or {}).get(tid)
            proxy = hist.volume_per_day * regional_share if hist else 0
            demand = min(proxy, own_rate) if own_rate is not None else proxy
            q['demand_basis'] = 'own hub sales capped by regional scenario' if own_rate is not None else 'regional share scenario; unproven at this hub'
            q['sales_per_day'] = demand
            q['daily_units'] = min(factory_capacity, lab_capacity, demand)
            q['bottleneck'] = min(('factory', factory_capacity), ('labs', lab_capacity), ('sales', demand), key=lambda p: p[1])[0]
            q['daily_profit'] = q['margin'] * q['daily_units'] if q['margin'] is not None else None
            q['profit_per_factory_hour'] = q['margin'] / q['factory_hours'] if q['margin'] is not None and q['factory_hours'] else None
        best = max(quotes, key=lambda q: q['daily_profit'] if q['daily_profit'] is not None else -q['cost_per_unit'])
        observed_jobs = [j for j in snap.jobs if j.get('blueprint_type_id') == recipe.blueprint_id]
        eligible_build = any(v is True for v in builders.values())
        eligible_inv = not source or any(v is True for v in best['inventors'].values())
        status = 'candidate' if eligible_build and eligible_inv else 'check builder/inventor eligibility'
        actual = (realized or {}).get(tid, {})
        actual_cost = actual.get('unit_cost')
        rows.append(dict(type_id=tid, product=name, status=status,
                         **best, variants=quotes, builders={names.get(c, str(c)): v for c, v in builders.items()},
                         list_price=listed, conservative_price=conservative, net_price=net,
                         region_daily_volume=hist.volume_per_day if hist else None,
                         input_capital_per_day=best['cost_per_unit'] * best['daily_units'],
                         own_hub_sales_per_day=(own_daily or {}).get(tid), realized=actual or None,
                         projected_minus_realized_unit_cost=best['cost_per_unit'] - actual_cost if actual_cost is not None else None,
                         existing_jobs=len(observed_jobs), book_as_of=market.iso_utc(quote.meta.last_modified)))
    rows.sort(key=lambda r: (r.get('daily_profit') is not None, r.get('daily_profit') or 0), reverse=True)
    return dict(generated=snap.now, hub=hub, input_hub=input_hub, site=site.name, items=rows, warnings=warnings, documents=snap.documents,
                assumptions=['Standalone line scenarios share the same slot budget; do not sum their daily profits.',
                             f'Sales capacity assumes {regional_share:.1%} of regional volume, not proven station demand.',
                             'Sale price is capped at the regional 30-day average; components bought at current asks.',
                             'Invention uses conservative SDE base chance times decryptor; no character chance bonus.',
                             'Invention eligibility is unknown with old SDE snapshots: update-data stores requirements.',
                             f'Copy budget is {copy_cost:g} ISK per invention attempt (or manufacturing copy); BPO and copying lab time excluded.',
                             'Unknown facility tax uses the NPC default; unknown rigs add no bonus.',
                             'Projected quotes use current asks/fees; realized figures use ledger weighted costs and observed fees, across destinations.',
                             'Daily capacity is a supplied slot scenario, conditional on eligible installers and available copies.',
                             'Measured build times include the historical copy TE; variants need a real-job timing check.'])
