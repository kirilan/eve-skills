"""Production rounds: immutable intentions, live reconciliation, and one shared material pool.

These functions do not mutate ESI or the ledger. The CLI persists plans and confirmed handoffs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import math

from . import exports, industry
from .cmd_restock import invention_requirements
from .production_data import free_slots, measured_hours
from .production_store import timestamp

ACTIVITIES = {'manufacturing': 1, 'research_time': 3, 'research_material': 4, 'copying': 5,
              'invention': 8, 'reaction': 9}
LOST = {'cancelled', 'reverted'}
DONE = LOST | {'delivered'}


def positive(value, name, *, integer=False, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RuntimeError(f'{name} must be a finite number')
    if value < 0 or (not zero and value == 0) or (integer and int(value) != value):
        raise RuntimeError(f'{name} must be {"a whole number" if integer else "positive"}')
    return int(value) if integer else value


def normalize(document, op, site, index, invention, resolve_type, resolve_character, now):
    if not isinstance(document, dict) or not isinstance(document.get('lines'), list) or not document['lines']:
        raise RuntimeError('round JSON needs a nonempty lines array')
    unknown = set(document) - {'title', 'start', 'lines'}
    if unknown:
        raise RuntimeError(f'unknown round fields: {sorted(unknown)}')
    start = timestamp(document.get('start') or now)
    lines = []
    for n, raw in enumerate(document['lines'], 1):
        allowed = {'product', 'installer', 'activity', 'runs', 'jobs', 'me', 'te', 'decryptor',
                   'blueprint_id', 'start', 'hours_per_run', 'probability', 'licensed_runs', 'prerequisites'}
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise RuntimeError(f'line {n}: unknown fields or not an object')
        try:
            product, name = resolve_type(raw['product'])
            installer, installer_name = resolve_character(raw['installer'])
            activity = raw.get('activity', 'manufacturing')
            aid = ACTIVITIES[activity]
            runs = positive(raw['runs'], 'runs', integer=True)
        except KeyError as err:
            raise RuntimeError(f'line {n}: missing or unsupported field {err}') from None
        recipe = index.get(product)
        if recipe is None:
            raise RuntimeError(f'{name}: no local recipe; run update-data')
        fac = op.facility(site.facility_for(activity))
        if not fac.location_id:
            raise RuntimeError(f'{fac.name}: location_id is required')
        bp = recipe.blueprint_id
        me, te = raw.get('me', 0), raw.get('te', 0)
        positive(me, 'me', integer=True, zero=True); positive(te, 'te', integer=True, zero=True)
        if me > 10 or te > 20:
            raise RuntimeError('ME must be 0..10 and TE 0..20')
        decryptor = raw.get('decryptor')
        output_runs = recipe.product_qty * runs
        probability = raw.get('probability')
        if probability is not None and not 0 < positive(probability, 'probability') <= 1:
            raise RuntimeError('probability must be in (0,1]')
        if activity == 'invention':
            source = next(((int(k), v, p) for k, v in invention['blueprints'].items()
                           for p in v['p'] if int(p[0]) == bp), None)
            if source is None:
                raise RuntimeError(f'{name}: no invention recipe')
            bp, inv, output = source
            need, _ = invention_requirements(invention, recipe.blueprint_id, runs, decryptor)
            dec = next((v for v in invention['decryptors'].values()
                        if str(v['name']).casefold() == (str(decryptor) + (' Decryptor' if decryptor and not str(decryptor).lower().endswith('decryptor') else '')).casefold()), {})
            output_runs = runs * (int(output[1]) + dec.get('runs', 0))
        elif activity in ('manufacturing', 'reaction'):
            if recipe.activity != activity:
                raise RuntimeError(f'{name}: recipe is {recipe.activity}, not {activity}')
            need = Counter({t: industry.required_quantity(q, runs, me, fac.material_multiplier(activity))
                            for t, q in recipe.materials.items()})
        else:
            # Science activities have fees but no material consumption in the local data.
            need = Counter()
        if not isinstance(raw.get('prerequisites', []), list) or any(not isinstance(v, str) for v in raw.get('prerequisites', [])):
            raise RuntimeError('prerequisites must be a list of text steps')
        if raw.get('blueprint_id') is not None:
            positive(raw['blueprint_id'], 'blueprint_id', integer=True)
        count = positive(raw.get('jobs', 1), 'jobs', integer=True)
        if count > 1000 or len(lines) + count > 1000:
            raise RuntimeError('a round supports at most 1000 jobs')
        if raw.get('blueprint_id') and count != 1:
            raise RuntimeError('one blueprint_id can identify only one job')
        for _ in range(count):
            line = dict(line_id=len(lines) + 1, product_id=product, product=name, installer_id=installer,
                        installer=installer_name, activity=activity, activity_id=aid,
                        facility_id=fac.location_id, facility=fac.label or fac.name, runs=runs,
                        blueprint_type_id=bp, output_blueprint_type_id=recipe.blueprint_id, blueprint_id=raw.get('blueprint_id'), me=me, te=te,
                        required_skills=(source[1].get('s') if activity == 'invention' else dict(recipe.skills) if recipe.skills is not None and activity in ('manufacturing', 'reaction') else None),
                        material_multiplier=fac.material_multiplier(activity),
                        materials={str(t): q for t, q in need.items()}, decryptor=decryptor,
                        start=timestamp(raw.get('start') or start), probability=probability,
                        expected_units=(output_runs * probability * recipe.product_qty if activity == 'invention'
                                        and probability is not None else output_runs if activity != 'invention' else None),
                        licensed_runs=raw.get('licensed_runs'), prerequisites=raw.get('prerequisites', []),
                        hours_per_run=raw.get('hours_per_run'), assumptions=list(fac.unknown))
            if line['hours_per_run'] is not None:
                positive(line['hours_per_run'], 'hours_per_run')
            if activity == 'copying':
                positive(line['licensed_runs'], 'licensed_runs', integer=True)
            lines.append(line)
    return dict(version=1, site=site.name, title=str(document.get('title') or 'Production round'),
                created_at=timestamp(now), start=start, lines=lines)


def reconcile(plan, jobs, jobs_as_of, *, since=None):
    """Global exact matching first, so a changed run count cannot steal another line's exact match."""
    candidates = sorted((j for j in jobs if j.get('status') not in LOST and
                         j.get('start_date', '') >= (since or plan['start'])), key=lambda j: j['job_id'])
    available = {j['job_id']: j for j in candidates}
    assigned = {}
    def same(line, j, exact):
        keys = ('installer_id', 'activity_id', 'blueprint_type_id', 'facility_id')
        if j.get('start_date', '') < line['start'] or any(line[k] != j.get(k) for k in keys):
            return False
        if line.get('blueprint_id') and line['blueprint_id'] != j.get('blueprint_id'):
            return False
        if line['activity_id'] == 8 and j.get('product_type_id') is not None:
            # Invention ESI output is a blueprint, checked by the caller's resolved output when available.
            if line.get('output_blueprint_type_id') and j['product_type_id'] != line['output_blueprint_type_id']:
                return False
        return not exact or (line['runs'] == j.get('runs') and
                             (line.get('licensed_runs') is None or line['licensed_runs'] == j.get('licensed_runs')))
    for exact in (True, False):
        for line in plan['lines']:
            if line['line_id'] in assigned:
                continue
            match = next((j for j in available.values() if same(line, j, exact)), None)
            if match:
                assigned[line['line_id']] = (match, 'installed' if exact else 'changed')
                del available[match['job_id']]
    result = []
    for line in plan['lines']:
        match, state = assigned.get(line['line_id'], (None, 'remaining'))
        if match is None and (jobs_as_of is None or jobs_as_of < line['start']):
            state = 'awaiting_refresh'
        result.append(dict(line, state=state, observed_job=match))
    return result, list(available.values())


def check_round(plan, snap, handoffs=()):
    lines, extras = reconcile(plan, snap.jobs, snap.as_of('jobs'))
    assets_as_of = snap.as_of('assets')
    warnings = list(snap.warnings)
    stocks, usable, owners = Counter(), Counter(), defaultdict(Counter)
    wanted_materials = {int(t) for row in lines if row['state'] not in ('installed', 'changed') for t in row['materials']}
    for a in snap.assets:
        if a['type_id'] not in wanted_materials and a['type_id'] not in {r['blueprint_type_id'] for r in lines}:
            continue
        key = (a['root'], int(a['type_id']))
        qty = max(0, a.get('quantity', 0))
        stocks[key] += qty
        if a.get('usable'):
            usable[key] += qty
        owners[a['root']][a['owner']] += qty
    # Never spend a pre-install asset balance a second time. Refresh, rather than guess ME/decryptors.
    facilities = {r['facility_id'] for r in lines}
    newer_jobs = [j for j in snap.jobs if j.get('facility_id') in facilities and
                  (assets_as_of is None or j.get('start_date', '') > assets_as_of)]
    stale = assets_as_of is None or bool(newer_jobs)
    if stale:
        warnings.append('assets predate jobs or have no timestamp; pool availability awaits an asset refresh')
    for h in handoffs:
        if assets_as_of is None or h['at'] > assets_as_of:
            warnings.append(f'user-confirmed whole-stack transfer to {h["holder_id"]} at {h["at"]}; not yet confirmed by ESI')
    need, reservations = Counter(), Counter()
    bp_reserved = set()
    busy = {j.get('blueprint_id') for j in snap.jobs if j.get('status') not in DONE}
    bps = []
    bp_as_of = snap.as_of('blueprints')
    for b in snap.blueprints:
        b = dict(b)
        if b.get('quantity') != -1 and bp_as_of:
            spent = sum(j['runs'] for j in snap.jobs if j.get('blueprint_id') == b['item_id'] and
                        j.get('status') == 'delivered' and (j.get('completed_date') or j.get('end_date') or '') > bp_as_of
                        and j['activity_id'] in (1, 8, 9))
            b['runs'] = b.get('runs', 0) - spent
        bps.append(b)
    slots_used = Counter()
    sequences = defaultdict(list)
    for row in lines:
        if row['state'] in ('installed', 'changed'):
            continue
        fac = row['facility_id']
        if row['installer'] not in sequences[fac]:
            sequences[fac].append(row['installer'])
        reasons = []
        local_need = {(fac, int(t)): q for t, q in row['materials'].items()}
        need.update(local_need)
        short = {t: q for (f, t), q in local_need.items() if stocks[(f, t)] - reservations[(f, t)] < q}
        reservations.update(local_need)  # reserve every intention, even blocked jobs
        if short and not stale and snap.complete('assets'):
            reasons.append('pool lacks materials: ' + ', '.join(f'{t}' for t in short))
        candidates = [b for b in bps if b['type_id'] == row['blueprint_type_id'] and b['item_id'] not in bp_reserved
                      and b['item_id'] not in busy and b['root'] == fac
                      and (not row.get('blueprint_id') or b['item_id'] == row['blueprint_id'])
                      and ((b.get('quantity') == -1 and row['activity_id'] != 8) or
                           (row['activity_id'] in (1, 8, 9) and b.get('runs', 0) >= row['runs']))
                      and (row['activity_id'] not in (1, 9) or b.get('material_efficiency', 0) == row['me'])]
        if candidates:
            bp = sorted(candidates, key=lambda b: (b.get('runs', 0), b['item_id']))[0]
            bp_reserved.add(bp['item_id'])
            row['selected_blueprint'] = dict(item_id=bp['item_id'], owner=bp['owner'], flag=bp['flag'])
            if bp['flag'] != 'Hangar' and not bp['flag'].startswith('CorpSAG'):
                reasons.append('move the blueprint from Deliveries/container into the item hangar')
        elif snap.complete('blueprints'):
            reasons.append('required idle blueprint not confirmed at this facility (runs/ME/holder may differ)')
        budget = free_slots(snap, row['installer_id'], row['activity_id'])
        key = (row['installer_id'], exports.SLOT_ACTIVITY.get(row['activity_id']))
        if budget is not None and slots_used[key] >= budget:
            reasons.append('deliver jobs or wait for a free slot')
        slots_used[key] += 1
        levels = snap.skills.get(row['installer_id'])
        required = row.get('required_skills')
        if levels is None or required is None:
            reasons.append('installer skill eligibility is unknown')
        elif any(levels.get(int(t), 0) < level for t, level in required.items()):
            reasons.append('installer lacks required skills')
        if any(usable[k] < reservations[k] for k in local_need) and not stale:
            reasons.append('move pool inputs from Deliveries/container into the item hangar before installing')
        if row.get('prerequisites'):
            reasons += row['prerequisites']
        if row['state'] != 'awaiting_refresh':
            row['state'] = 'awaiting_refresh' if stale or not snap.complete('blueprints') else 'blocked' if reasons else 'remaining'
        row['blockers'] = reasons
        h = measured_hours(snap.jobs, row['installer_id'], row['activity_id'], row['blueprint_type_id'], fac)
        row['timing_basis'] = 'measured at this facility' if h else 'plan estimate' if row.get('hours_per_run') else 'unknown'
        h = h or row.get('hours_per_run')
        start = max(snap.now, row['start'])
        row['earliest_start'] = start
        row['estimated_end'] = timestamp((datetime.fromisoformat(start.replace('Z', '+00:00')) +
                                         timedelta(hours=h * row['runs'])).isoformat()) if h else None
    pool = [dict(facility_id=f, type_id=t, need=q,
                 held=stocks[(f, t)] if snap.complete('assets') else None,
                 usable=usable[(f, t)] if snap.complete('assets') else None,
                 move_to_hangar=max(0, stocks[(f, t)] - usable[(f, t)]) if snap.complete('assets') else None,
                 short=max(0, q - stocks[(f, t)]) if snap.complete('assets') and not stale else None)
            for (f, t), q in sorted(need.items())]
    holders = []
    for f in sequences:
        c = owners[f]
        copies = Counter(b['owner'] for b in bps if b['root'] == f and b['type_id'] in
                         {r['blueprint_type_id'] for r in lines if r['facility_id'] == f})
        latest = next((h for h in reversed(handoffs) if h['facility_id'] == f), None)
        holders.append(dict(facility_id=f, holders=dict(c), blueprint_holders=dict(copies),
                            bulk_holder=max(c, key=c.get) if c else None, confirmed_handoff=latest))
        if len(set(c) | set(copies)) > 1:
            warnings.append(f'pool/copies at {f} are scattered across holders; consolidate the whole stack before passing it')
    for b in bps:
        if b['type_id'] in {r['blueprint_type_id'] for r in lines} and b['root'] not in facilities:
            warnings.append(f'blueprint {b["item_id"]} is at {b["root"]}, outside the planned facilities')
    return dict(round_id=plan.get('id'), title=plan['title'], site=plan['site'], generated=snap.now,
                lines=lines, unplanned_jobs=extras, pool=pool, holders=holders,
                installer_sequence=dict(sequences), confirmed_handoffs=list(handoffs), documents=snap.documents,
                warnings=warnings, assumptions=['The whole pool passes between installers; no per-character material batches.',
                'Finish times assume a free slot and all listed prerequisites completed at earliest_start.',
                'A recent install may remain invisible until the next ESI jobs refresh.'])
