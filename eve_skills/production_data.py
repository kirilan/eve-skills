"""Live read adapter shared by production planning reports. Missing documents stay unknown."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import statistics

from . import esi as esi_mod, exports, market, sso
from .cmd_restock import _roots
from .production_store import timestamp


@dataclass
class Snapshot:
    now: str
    assets: list[dict] = field(default_factory=list)
    blueprints: list[dict] = field(default_factory=list)
    jobs: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    skills: dict[int, dict | None] = field(default_factory=dict)
    characters: dict[int, str] = field(default_factory=dict)
    documents: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def complete(self, section):
        docs = [d for d in self.documents if d['section'] == section]
        return bool(docs) and all(d['available'] for d in docs)

    def as_of(self, section):
        docs = [d for d in self.documents if d['section'] == section]
        if not docs or any(not d['available'] or not d['as_of'] for d in docs):
            return None
        return min(d['as_of'] for d in docs)


def read_site(client, site, *, extra_characters=(), market_characters=(), with_orders=False):
    snap = Snapshot(timestamp(client.now().isoformat()))
    tokens = []
    site_ids, extra_ids, market_ids = set(), set(), set()
    for name in dict.fromkeys((*site.characters, *site.readers, *extra_characters, *market_characters)):
        try:
            cid = sso.resolve_character(name)
            tok = sso.get_access_token(cid)
        except (RuntimeError, KeyError, OSError) as err:
            snap.warnings.append(f'{name}: {err}')
            sections = ['assets']
            if name not in market_characters or name in site.characters or name in site.readers or name in extra_characters:
                sections += ['blueprints', 'jobs', 'skills']
            if with_orders:
                sections.append('orders')
            for section in sections:
                snap.documents.append(dict(owner=name, section=section, available=False, as_of=None, expires=None))
            continue
        if name in site.characters or name in site.readers:
            site_ids.add(cid)
        if name in extra_characters:
            extra_ids.add(cid)
        if name in market_characters:
            market_ids.add(cid)
        snap.characters[cid] = tok.get('character_name') or name
        tokens.append(tok)
    if not tokens:
        raise RuntimeError('no site characters readable; check operation show and logins')

    def read(owner, section, path, candidates, scope, pages=True):
        for tok in candidates:
            if scope not in (tok.get('scopes') or []):
                continue
            try:
                method = client.get_all_meta if pages else client.get_meta
                rows, meta = method(path, token=tok['access_token'])
                snap.documents.append(dict(owner=owner, section=section, available=True,
                                           as_of=market.iso_utc(meta.last_modified), expires=market.iso_utc(meta.expires)))
                return rows
            except (esi_mod.EsiError, RuntimeError) as err:
                snap.warnings.append(f'{owner} {section}: {err}')
        snap.documents.append(dict(owner=owner, section=section, available=False, as_of=None, expires=None))
        snap.warnings.append(f'{owner}: {section} unavailable (check consent and access); quantities are unknown')
        return None

    for tok in tokens:
        cid = int(tok['character_id'])
        if cid not in site_ids | extra_ids:
            continue
        skills = read(cid, 'skills', f'/characters/{cid}/skills', [tok], 'esi-skills.read_skills.v1', False)
        snap.skills[cid] = ({int(r['skill_id']): int(r.get('active_skill_level', 0))
                             for r in skills['skills']} if skills is not None else None)
        jobs = read(cid, 'jobs', f'/characters/{cid}/industry/jobs?include_completed=true', [tok],
                    'esi-industry.read_character_jobs.v1', False)
        snap.jobs += [dict(j, owner=f'char:{cid}') for j in jobs or []]

    owners = []
    if site.owner == 'personal':
        owners = [(f'char:{t["character_id"]}', f'/characters/{t["character_id"]}', [t], False) for t in tokens]
    else:
        corps = {}
        for t in tokens:
            if int(t['character_id']) not in site_ids:
                continue
            try:
                cid = client.get(f'/characters/{t["character_id"]}')['corporation_id']
                corps.setdefault(cid, []).append(t)
            except (esi_mod.EsiError, KeyError) as err:
                raise RuntimeError(f'cannot identify the site corporation: {err}') from err
        if len(corps) != 1:
            raise RuntimeError('a corporation site must have readers from one corporation')
        owners = [(f'corp:{cid}', f'/corporations/{cid}', ts, True) for cid, ts in corps.items()]
        # A site's seller may hold personal stock/orders even when production is corporate.
        owners += [(f'char:{t["character_id"]}', f'/characters/{t["character_id"]}', [t], False)
                   for t in tokens if int(t['character_id']) in market_ids]
    for owner, base, candidates, corp in owners:
        assets = read(owner, 'assets', base + '/assets', candidates,
                      'esi-assets.read_corporation_assets.v1' if corp else 'esi-assets.read_assets.v1')
        if with_orders:
            orders = read(owner, 'orders', base + '/orders', candidates,
                          'esi-markets.read_corporation_orders.v1' if corp else 'esi-markets.read_character_orders.v1')
            snap.orders += [dict(o, owner=owner, state='open') for o in orders or []]
        market_only = not corp and int(candidates[0]['character_id']) not in site_ids | extra_ids
        bps = ([] if market_only else read(owner, 'blueprints', base + '/blueprints', candidates,
                   'esi-corporations.read_blueprints.v1' if corp else 'esi-characters.read_blueprints.v1'))
        if corp:
            jobs = read(owner, 'jobs', base + '/industry/jobs?include_completed=true', candidates,
                        'esi-industry.read_corporation_jobs.v1')
            snap.jobs += [dict(j, owner=owner) for j in jobs or []]
        roots = _roots(assets or [])
        asset_by_id = {a['item_id']: a for a in assets or []}
        for a in assets or []:
            # Container contents need an explicit move into the install hangar too.
            flag = a.get('location_flag', '')
            snap.assets.append(dict(a, owner=owner, root=roots.get(a['location_id'], a['location_id']),
                                    usable=flag == 'Hangar' or flag.startswith('CorpSAG')))
        for b in bps or []:
            asset = asset_by_id.get(b['item_id'], {})
            snap.blueprints.append(dict(b, owner=owner, root=roots.get(b['location_id'], b['location_id']),
                                        flag=asset.get('location_flag', b.get('location_flag', 'unknown'))))
    snap.jobs = list({j['job_id']: j for j in snap.jobs}.values())
    snap.orders = list({o['order_id']: o for o in snap.orders}.values())
    return snap


def measured_hours(jobs, installer, activity, blueprint, facility):
    values = []
    for j in jobs:
        if (j.get('installer_id'), j.get('activity_id'), j.get('blueprint_type_id'), j.get('facility_id')) != \
                (installer, activity, blueprint, facility) or j.get('status') not in ('active', 'ready', 'delivered'):
            continue
        if not j.get('start_date') or not j.get('end_date') or not j.get('runs'):
            continue
        seconds = (datetime.fromisoformat(j['end_date'].replace('Z', '+00:00')) -
                   datetime.fromisoformat(j['start_date'].replace('Z', '+00:00'))).total_seconds()
        if seconds > 0:
            values.append(seconds / 3600 / j['runs'])
    return statistics.median(values) if values else None


def free_slots(snap, installer, activity):
    kind = exports.SLOT_ACTIVITY.get(activity)
    levels = snap.skills.get(installer)
    if kind is None or levels is None or not snap.complete('jobs'):
        return None
    basic, advanced = exports.SLOT_SKILLS[kind]
    used = sum(j.get('installer_id') == installer and exports.SLOT_ACTIVITY.get(j['activity_id']) == kind
               and j.get('status') not in ('delivered', 'cancelled', 'reverted') for j in snap.jobs)
    return max(0, 1 + levels.get(basic, 0) + levels.get(advanced, 0) - used)
