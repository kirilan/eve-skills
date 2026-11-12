"""Saved intentions and dated costing evidence. No ESI writes; facts remain in the ledger."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE production_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    document TEXT NOT NULL
);
CREATE UNIQUE INDEX production_active_site ON production_rounds(site) WHERE closed_at IS NULL;
CREATE TABLE pool_handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES production_rounds(id),
    facility_id INTEGER NOT NULL,
    holder_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    hangar TEXT NOT NULL,
    note TEXT NOT NULL
);
CREATE TABLE facility_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_id INTEGER NOT NULL,
    effective_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    profile TEXT NOT NULL,
    UNIQUE(facility_id, effective_at)
);
CREATE TRIGGER facility_history_no_update BEFORE UPDATE ON facility_history
BEGIN SELECT RAISE(ABORT, 'facility history is immutable; append a dated correction'); END;
CREATE TRIGGER facility_history_no_delete BEFORE DELETE ON facility_history
BEGIN SELECT RAISE(ABORT, 'facility history is immutable'); END;
"""


def timestamp(value=None):
    if value is None:
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (TypeError, ValueError):
        raise RuntimeError(f'expected an ISO timestamp with a time zone, got {value!r}') from None


def save_round(conn, doc):
    if conn.execute('SELECT id FROM production_rounds WHERE site=? AND closed_at IS NULL', (doc['site'],)).fetchone():
        raise RuntimeError('this site already has an active round; close it before saving another')
    with conn:
        cur = conn.execute('INSERT INTO production_rounds(site,title,created_at,document) VALUES(?,?,?,?)',
                           (doc['site'], doc['title'], doc['created_at'], json.dumps(doc)))
    return cur.lastrowid


def get_round(conn, ident=None, site=None):
    row = (conn.execute('SELECT * FROM production_rounds WHERE id=?', (ident,)).fetchone() if ident else
           conn.execute('SELECT * FROM production_rounds WHERE site=? AND closed_at IS NULL', (site,)).fetchone())
    if row is None:
        raise RuntimeError('no matching production round; save one with production round save')
    return {**json.loads(row['document']), 'id': row['id'], 'closed_at': row['closed_at']}


def histories(conn):
    return [{**dict(r), 'profile': json.loads(r['profile'])}
            for r in conn.execute('SELECT * FROM facility_history ORDER BY effective_at,id')]


def profile_at(history, facility_id, at):
    return next((r for r in reversed(history) if r['facility_id'] == facility_id and r['effective_at'] <= at), None)


def record_facility(conn, facility_id, effective_at, profile, reason, recorded_at=None):
    effective_at = timestamp(effective_at)
    if not reason.strip():
        raise RuntimeError('a facility observation needs a reason/source')
    for kind in ('material', 'time'):
        for value in profile.get(kind, {}).values():
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1:
                raise RuntimeError('facility multipliers must be finite and in (0, 1]')
    with conn:
        conn.execute('INSERT INTO facility_history(facility_id,effective_at,recorded_at,reason,profile) '
                     'VALUES(?,?,?,?,?)', (facility_id, effective_at, timestamp(recorded_at), reason,
                                          json.dumps(profile, sort_keys=True)))


def observe_facilities(conn, op, at):
    """A sync records configuration changes effective NOW, never at an old job's start."""
    from dataclasses import asdict
    history = histories(conn)
    profiles = {}
    for f in op.facilities.values():
        if f.location_id is None:
            continue
        # Identity labels do not change the physical facility's costing evidence.
        profile = json.loads(json.dumps(asdict(f)))
        for key in ('name', 'label'):
            profile.pop(key, None)
        if f.location_id in profiles and profiles[f.location_id] != profile:
            raise RuntimeError(f'conflicting facility aliases at {f.location_id}; use one costing profile per location')
        profiles[f.location_id] = profile
    for location_id, profile in profiles.items():
        previous = profile_at(history, location_id, at)
        if previous is None or {k: v for k, v in previous['profile'].items() if k not in ('name', 'label')} != profile:
            record_facility(conn, location_id, at, profile, 'operation configuration observed at ledger sync', at)
