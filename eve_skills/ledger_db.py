"""The accounting ledger's local store: one SQLite file of raw ESI facts. Standard library only.

ESI forgets. Wallet transactions and journal entries are served for 30 days, completed industry jobs
for about 90, and a blueprint copy vanishes from `/blueprints` the moment its last run is used. The
ledger's whole job is to keep those rows after ESI has let go of them, so this file is the one place
in the package holding data that cannot be downloaded again - which is why it lives in the *data*
directory and not the cache.

What is stored is facts, never conclusions: jobs, transactions, journal entries, orders, blueprint
sightings and reference prices, each keyed by ESI's own id and written with "insert, or refresh the
mutable columns". A sync can therefore run any number of times, from `watch` and by hand at once,
and never double-count. Costs, margins and valuations are computed from these rows on every report
(`ledger.py`), so a better costing rule later re-prices all of history instead of only what comes
after it.

Concurrency: WAL journal mode lets a report read while a sync writes, and a busy timeout makes two
writers queue instead of failing. Each sync commits once per document, so a crash loses at most the
document in flight - and the next sync fetches it again.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping

from . import paths

FILE_NAME = "ledger.sqlite3"

# Bumped with every schema change; `_migrate` walks an older file forward one step at a time.
SCHEMA_VERSION = 2

# ESI serves this many days of wallet journal and transactions; a row older than that which no sync
# stored is gone for good. Reports and `doctor` warn once the last sync is STALE_SYNC_DAYS old, which
# still leaves a missed week to catch up in.
WALLET_HISTORY_DAYS = 30
STALE_SYNC_DAYS = 20

# Seconds a writer waits for another writer before giving up. A sync commits documents of a few
# thousand rows, so anything past a few seconds means a stuck process, not contention.
BUSY_TIMEOUT_MS = 30_000

_SCHEMA_V1 = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per industry job, whoever owns it. `owner` is 'corp:<id>' or 'char:<id>'. Status,
-- successful_runs and completed_date change while ESI still serves the job, so they are refreshed.
CREATE TABLE jobs (
    job_id            INTEGER PRIMARY KEY,
    owner             TEXT NOT NULL,
    activity_id       INTEGER NOT NULL,
    status            TEXT,
    installer_id      INTEGER,
    facility_id       INTEGER,
    blueprint_id      INTEGER,
    blueprint_type_id INTEGER,
    product_type_id   INTEGER,
    runs              INTEGER NOT NULL,
    licensed_runs     INTEGER,
    successful_runs   INTEGER,
    probability       REAL,
    cost              REAL,
    start_date        TEXT NOT NULL,
    end_date          TEXT,
    completed_date    TEXT,
    raw               TEXT NOT NULL,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL
);

-- Market transactions. `wallet` is 'corp:<id>:<division>' or 'char:<id>'; the same trade between two
-- of our own wallets is two rows, which is exactly how the engine recognises an internal transfer.
CREATE TABLE transactions (
    wallet         TEXT NOT NULL,
    transaction_id INTEGER NOT NULL,
    date           TEXT NOT NULL,
    type_id        INTEGER NOT NULL,
    quantity       INTEGER NOT NULL,
    unit_price     REAL NOT NULL,
    is_buy         INTEGER NOT NULL,
    client_id      INTEGER,
    location_id    INTEGER,
    journal_ref_id INTEGER,
    raw            TEXT NOT NULL,
    PRIMARY KEY (wallet, transaction_id)
);

-- Wallet journal. One ref id can appear in two wallets (a transfer between them), hence the pair key.
CREATE TABLE journal (
    wallet          TEXT NOT NULL,
    id              INTEGER NOT NULL,
    date            TEXT NOT NULL,
    ref_type        TEXT NOT NULL,
    amount          REAL,
    context_id      INTEGER,
    context_id_type TEXT,
    first_party_id  INTEGER,
    second_party_id INTEGER,
    raw             TEXT NOT NULL,
    PRIMARY KEY (wallet, id)
);

-- Market orders, open and historical. Kept so broker fees - which the journal does not link to any
-- order - can be matched to the order whose `issued` stamp they share.
CREATE TABLE orders (
    order_id      INTEGER PRIMARY KEY,
    owner         TEXT NOT NULL,
    type_id       INTEGER NOT NULL,
    is_buy        INTEGER NOT NULL,
    issued        TEXT NOT NULL,
    issued_by     INTEGER,
    price         REAL,
    volume_total  INTEGER,
    volume_remain INTEGER,
    state         TEXT,
    location_id   INTEGER,
    raw           TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

-- Every blueprint seen by a sync. An invented copy's ME/TE is how the ledger learns which decryptor
-- made it; ESI's job rows never say.
CREATE TABLE blueprints (
    item_id    INTEGER PRIMARY KEY,
    owner      TEXT NOT NULL,
    type_id    INTEGER NOT NULL,
    quantity   INTEGER,
    me         INTEGER,
    te         INTEGER,
    runs       INTEGER,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

-- CCP's reference prices, one row per type per UTC day, only for types the ledger has met: the
-- fallback opening price, and a dated record of what the market thought of each type.
CREATE TABLE prices (
    type_id        INTEGER NOT NULL,
    day            TEXT NOT NULL,
    average_price  REAL,
    adjusted_price REAL,
    PRIMARY KEY (type_id, day)
);

-- What stock the ledger never saw arrive is worth, per type: fixed once, at the cutover. `source` says
-- which figure it is ('forge_history' - the median daily average in The Forge over the 30 days up to
-- the cutover - or 'ccp_average' when that region never traded the type), `basis` the day it describes.
CREATE TABLE opening_prices (
    type_id INTEGER PRIMARY KEY,
    price   REAL NOT NULL,
    source  TEXT NOT NULL,
    basis   TEXT NOT NULL,
    fetched TEXT NOT NULL
);

-- One row per document read by a sync, so `doctor` and `ledger sync` can say how far back each
-- source reaches and when it was last refreshed.
CREATE TABLE sync_log (
    source     TEXT NOT NULL,
    synced_at  TEXT NOT NULL,
    rows_seen  INTEGER NOT NULL,
    rows_new   INTEGER NOT NULL,
    oldest     TEXT,
    newest     TEXT,
    PRIMARY KEY (source, synced_at)
);

CREATE INDEX transactions_date ON transactions (date);
CREATE INDEX journal_date ON journal (date);
CREATE INDEX journal_context ON journal (context_id_type, context_id);
CREATE INDEX blueprints_type ON blueprints (type_id);
"""


# Version 2: the operation's own log. Notes are what a person or an agent wrote down - dated updates,
# decisions, incidents, analyses and to-dos - and snapshots are the figures every sync records, so
# progress can be read back as a series instead of reconstructed from prose.
_SCHEMA_V2 = """
CREATE TABLE notes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    title     TEXT NOT NULL,
    body      TEXT NOT NULL DEFAULT '',
    status    TEXT,
    closed_at TEXT,
    source    TEXT
);

CREATE TABLE snapshots (
    synced_at TEXT PRIMARY KEY,
    data      TEXT NOT NULL
);

CREATE INDEX notes_ts ON notes (ts);
"""

# What a note can be. A to-do is the only kind with a status: open until `done` closes it.
NOTE_KINDS = ("update", "decision", "todo", "incident", "analysis")


def db_path(create: bool = True) -> str:
    """The ledger file; create=False resolves the path without touching disk."""
    return os.path.join(paths.data_dir(create=create), FILE_NAME)


def exists() -> bool:
    return os.path.isfile(db_path(create=False))


def connect(path: str | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    """An open ledger, migrated to SCHEMA_VERSION; `readonly` never creates or migrates anything.

    Read-only is what `doctor` uses: it must not create a ledger for somebody who never asked for
    one, and must not upgrade a file another version of the tool may still be writing."""
    target = path or db_path(create=not readonly)
    if readonly:
        if not os.path.isfile(target):
            raise FileNotFoundError(target)
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    else:
        conn = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"the ledger at {db_path(create=False)} was written by a newer eve-skills "
                           f"(schema {version}, this one knows {SCHEMA_VERSION}) - upgrade eve-skills")
    if version < 1:
        with conn:
            conn.executescript(_SCHEMA_V1)
            conn.execute("PRAGMA user_version = 1")
    if version < 2:
        with conn:
            conn.executescript(_SCHEMA_V2)
            conn.execute("PRAGMA user_version = 2")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


def _int(value) -> int | None:
    try:
        return None if value is None or isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None


def _raw(row: Mapping) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def _count_new(conn: sqlite3.Connection, table: str, where: str, keys: list[tuple]) -> int:
    """How many of `keys` the table does not hold yet - counted before the write, so a report can say
    "12 new" without a second query per row."""
    if not keys:
        return 0
    known = 0
    for key in keys:
        if conn.execute(f"SELECT 1 FROM {table} WHERE {where}", key).fetchone():
            known += 1
    return len(keys) - known


def upsert_jobs(conn: sqlite3.Connection, owner: str, rows: Iterable[Mapping], seen_at: str) -> int:
    """Store job rows; returns how many were new. Rows without an id or start are skipped."""
    usable = [r for r in rows if _int(r.get("job_id")) is not None and r.get("start_date")
              and _int(r.get("activity_id")) is not None and _int(r.get("runs")) is not None]
    new = _count_new(conn, "jobs", "job_id = ?", [(int(r["job_id"]),) for r in usable])
    conn.executemany(
        """INSERT INTO jobs (job_id, owner, activity_id, status, installer_id, facility_id, blueprint_id,
               blueprint_type_id, product_type_id, runs, licensed_runs, successful_runs, probability, cost,
               start_date, end_date, completed_date, raw, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (job_id) DO UPDATE SET
               status = excluded.status, successful_runs = COALESCE(excluded.successful_runs, successful_runs),
               end_date = excluded.end_date, completed_date = COALESCE(excluded.completed_date, completed_date),
               cost = excluded.cost, raw = excluded.raw, last_seen = excluded.last_seen""",
        [(int(r["job_id"]), owner, int(r["activity_id"]), r.get("status"), _int(r.get("installer_id")),
          _int(r.get("facility_id")), _int(r.get("blueprint_id")), _int(r.get("blueprint_type_id")),
          _int(r.get("product_type_id")), int(r["runs"]), _int(r.get("licensed_runs")),
          _int(r.get("successful_runs")), _float(r.get("probability")), _float(r.get("cost")),
          r["start_date"], r.get("end_date"), r.get("completed_date"), _raw(r), seen_at, seen_at)
         for r in usable])
    return new


def insert_transactions(conn: sqlite3.Connection, wallet: str, rows: Iterable[Mapping]) -> int:
    """Store transactions (they never change once written); returns how many were new."""
    usable = [r for r in rows if _int(r.get("transaction_id")) is not None and r.get("date")
              and _int(r.get("type_id")) is not None and _int(r.get("quantity")) is not None
              and _float(r.get("unit_price")) is not None]
    new = _count_new(conn, "transactions", "wallet = ? AND transaction_id = ?",
                     [(wallet, int(r["transaction_id"])) for r in usable])
    conn.executemany(
        """INSERT OR IGNORE INTO transactions (wallet, transaction_id, date, type_id, quantity, unit_price,
               is_buy, client_id, location_id, journal_ref_id, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(wallet, int(r["transaction_id"]), r["date"], int(r["type_id"]), int(r["quantity"]),
          float(r["unit_price"]), 1 if r.get("is_buy") else 0, _int(r.get("client_id")),
          _int(r.get("location_id")), _int(r.get("journal_ref_id")), _raw(r)) for r in usable])
    return new


def insert_journal(conn: sqlite3.Connection, wallet: str, rows: Iterable[Mapping]) -> int:
    """Store journal entries (immutable once written); returns how many were new."""
    usable = [r for r in rows if _int(r.get("id")) is not None and r.get("date") and r.get("ref_type")]
    new = _count_new(conn, "journal", "wallet = ? AND id = ?", [(wallet, int(r["id"])) for r in usable])
    conn.executemany(
        """INSERT OR IGNORE INTO journal (wallet, id, date, ref_type, amount, context_id, context_id_type,
               first_party_id, second_party_id, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(wallet, int(r["id"]), r["date"], str(r["ref_type"]), _float(r.get("amount")),
          _int(r.get("context_id")), r.get("context_id_type"), _int(r.get("first_party_id")),
          _int(r.get("second_party_id")), _raw(r)) for r in usable])
    return new


def upsert_orders(conn: sqlite3.Connection, owner: str, rows: Iterable[Mapping], seen_at: str) -> int:
    """Store order rows, open or historical; the newest sighting's volumes and state win."""
    usable = [r for r in rows if _int(r.get("order_id")) is not None and r.get("issued")
              and _int(r.get("type_id")) is not None]
    new = _count_new(conn, "orders", "order_id = ?", [(int(r["order_id"]),) for r in usable])
    conn.executemany(
        """INSERT INTO orders (order_id, owner, type_id, is_buy, issued, issued_by, price, volume_total,
               volume_remain, state, location_id, raw, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (order_id) DO UPDATE SET
               issued = excluded.issued, price = excluded.price, volume_remain = excluded.volume_remain,
               state = excluded.state, raw = excluded.raw, last_seen = excluded.last_seen""",
        [(int(r["order_id"]), owner, int(r["type_id"]), 1 if r.get("is_buy_order") else 0, r["issued"],
          _int(r.get("issued_by")), _float(r.get("price")), _int(r.get("volume_total")),
          _int(r.get("volume_remain")), r.get("state") or "open", _int(r.get("location_id")), _raw(r), seen_at)
         for r in usable])
    return new


def upsert_blueprints(conn: sqlite3.Connection, owner: str, rows: Iterable[Mapping], seen_at: str) -> int:
    """Record blueprint sightings; ME/TE/runs are refreshed, `first_seen` never moves."""
    usable = [r for r in rows if _int(r.get("item_id")) is not None and _int(r.get("type_id")) is not None]
    new = _count_new(conn, "blueprints", "item_id = ?", [(int(r["item_id"]),) for r in usable])
    conn.executemany(
        """INSERT INTO blueprints (item_id, owner, type_id, quantity, me, te, runs, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (item_id) DO UPDATE SET
               owner = excluded.owner, quantity = excluded.quantity, me = excluded.me, te = excluded.te,
               runs = excluded.runs, last_seen = excluded.last_seen""",
        [(int(r["item_id"]), owner, int(r["type_id"]), _int(r.get("quantity")),
          _int(r.get("material_efficiency")), _int(r.get("time_efficiency")), _int(r.get("runs")),
          seen_at, seen_at) for r in usable])
    return new


def insert_prices(conn: sqlite3.Connection, day: str, rows: Iterable[tuple[int, float | None, float | None]]) -> int:
    """(type id, average, adjusted) for one UTC day; the first sync of a day is the one kept."""
    usable = list(rows)
    new = _count_new(conn, "prices", "type_id = ? AND day = ?", [(t, day) for t, _a, _j in usable])
    conn.executemany("INSERT OR IGNORE INTO prices (type_id, day, average_price, adjusted_price) "
                     "VALUES (?, ?, ?, ?)", [(t, day, a, j) for t, a, j in usable])
    return new


def opening_price_types(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT type_id FROM opening_prices")}


def insert_opening_prices(conn: sqlite3.Connection, rows: Iterable[tuple[int, float, str, str]],
                          fetched: str) -> int:
    """(type id, price, source, basis day); an opening price, once written, never changes."""
    usable = list(rows)
    new = _count_new(conn, "opening_prices", "type_id = ?", [(t,) for t, *_rest in usable])
    conn.executemany("INSERT OR IGNORE INTO opening_prices (type_id, price, source, basis, fetched) "
                     "VALUES (?, ?, ?, ?, ?)", [(t, p, src, basis, fetched) for t, p, src, basis in usable])
    return new


def cutover(conn: sqlite3.Connection) -> str | None:
    """The day the ledger's history starts, fixed by the first sync that found any: stock older than
    this was not seen arriving and is valued at market as of this day."""
    return get_meta(conn, "cutover")


def add_note(conn: sqlite3.Connection, ts: str, kind: str, title: str, body: str = "",
             source: str | None = None) -> int:
    """Store one note; returns its id. A to-do starts open."""
    if kind not in NOTE_KINDS:
        raise RuntimeError(f"unknown note kind '{kind}' - choices: {', '.join(NOTE_KINDS)}")
    if not title.strip():
        raise RuntimeError("a note needs a title")
    cur = conn.execute("INSERT INTO notes (ts, kind, title, body, status, source) VALUES (?, ?, ?, ?, ?, ?)",
                       (ts, kind, title.strip(), body, "open" if kind == "todo" else None, source))
    return int(cur.lastrowid)


def notes(conn: sqlite3.Connection, *, kind: str | None = None, since: str | None = None,
          open_only: bool = False, text: str | None = None, limit: int | None = None) -> list[dict]:
    """Notes newest first, filtered; `text` matches title or body, case-insensitively."""
    where, args = [], []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if since:
        where.append("ts >= ?")
        args.append(since)
    if open_only:
        where.append("status = 'open'")
    if text:
        where.append("(title LIKE ? OR body LIKE ?)")
        args += [f"%{text}%"] * 2
    sql = "SELECT * FROM notes" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, args)]


def note(conn: sqlite3.Connection, note_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row) if row else None


def close_note(conn: sqlite3.Connection, note_id: int, closed_at: str) -> dict:
    """Mark a to-do done; any other kind, or an unknown id, is an error rather than a silent no-op."""
    row = note(conn, note_id)
    if row is None:
        raise RuntimeError(f"no note {note_id}")
    if row["kind"] != "todo":
        raise RuntimeError(f"note {note_id} is a {row['kind']}, not a to-do")
    conn.execute("UPDATE notes SET status = 'done', closed_at = ? WHERE id = ?", (closed_at, note_id))
    return note(conn, note_id)


def store_snapshot(conn: sqlite3.Connection, synced_at: str, data: Mapping) -> None:
    conn.execute("INSERT OR REPLACE INTO snapshots (synced_at, data) VALUES (?, ?)",
                 (synced_at, json.dumps(data, sort_keys=True)))


def snapshots(conn: sqlite3.Connection, since: str | None = None) -> list[dict]:
    """Stored snapshots oldest first, each as {"synced_at": ..., **figures}."""
    rows = conn.execute("SELECT synced_at, data FROM snapshots WHERE synced_at >= ? ORDER BY synced_at",
                        (since or "",))
    return [{"synced_at": r[0], **json.loads(r[1])} for r in rows]


def log_sync(conn: sqlite3.Connection, source: str, synced_at: str, seen: int, new: int,
             oldest: str | None, newest: str | None) -> None:
    conn.execute("INSERT OR REPLACE INTO sync_log (source, synced_at, rows_seen, rows_new, oldest, newest) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (source, synced_at, seen, new, oldest, newest))


def last_sync(conn: sqlite3.Connection) -> str | None:
    """When the most recent document was synced, as ESI's ISO stamp; None before the first sync."""
    return get_meta(conn, "last_sync")


def sources(conn: sqlite3.Connection) -> list[dict]:
    """Per source: its latest sync, how far back everything stored from it reaches, and rows held."""
    out = []
    for row in conn.execute("SELECT source, MAX(synced_at) AS synced_at, MIN(oldest) AS oldest, "
                            "MAX(newest) AS newest FROM sync_log GROUP BY source ORDER BY source"):
        out.append(dict(row))
    return out
