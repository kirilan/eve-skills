"""`ledger sql` and `ledger schema`: read-only questions to the ledger file, without a `sqlite3` binary.

The file is opened read-only (`file:...?mode=ro`, plus `PRAGMA query_only`) and only one `SELECT` or
`WITH` statement is accepted, so a query can never change the books. Three SQL functions cover the
look-ups sessions kept rebuilding by hand:

- `type_name(type_id)`: the local type catalogue (the name and type caches); unknown -> `type <id>`.
- `character_name(id)`: the stored characters; unknown -> the id as text.
- `local_time(ts[, zone])`: an ISO UTC stamp -> `YYYY-MM-DD HH:MM` in the zone (default: the operation
  file's `planning.timezone`, else UTC).

All three read local files only: a query over thousands of rows makes no ESI request.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

from . import esi as esi_mod, ledger_db, paths, render, sso, universe

DEFAULT_LIMIT = 200
_COMMENT = re.compile(r"(--[^\n]*(\n|$))|(/\*.*?\*/)", re.DOTALL)


def connect() -> sqlite3.Connection:
    """The ledger opened for reading only; never created, never migrated."""
    try:
        conn = ledger_db.connect(readonly=True)
    except FileNotFoundError:
        raise RuntimeError("no ledger yet - run: eve-skills ledger sync") from None
    conn.execute("PRAGMA query_only = ON")
    return conn


def check_statement(sql: str) -> str:
    """The statement, stripped, when it is one SELECT or WITH; otherwise a RuntimeError saying why."""
    text = sql.strip().rstrip(";").strip()
    bare = _COMMENT.sub(" ", text).strip()
    if not bare:
        raise RuntimeError("empty query")
    keyword = bare.split(None, 1)[0].upper()
    if keyword not in ("SELECT", "WITH"):
        raise RuntimeError(f"ledger sql runs one SELECT or WITH statement only, not {keyword}")
    if not sqlite3.complete_statement(text + ";") or ";" in _strip_literals(bare):
        raise RuntimeError("ledger sql runs exactly one statement")
    return text


def _strip_literals(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", "''", sql)


def _type_names() -> dict[int, str]:
    names: dict[int, str] = {}
    try:
        names.update(esi_mod._read_name_cache(os.path.join(paths.cache_dir(), "names.json")))
    except Exception:          # a damaged cache costs names, never the query
        pass
    for key, record in universe.read_doc()["types"].items():
        if record.get("name"):
            names[int(key)] = record["name"]
    return names


def _character_names() -> dict[int, str]:
    names = {}
    for record in sso.list_characters():
        if record.get("character_id") and record.get("character_name"):
            names[int(record["character_id"])] = record["character_name"]
    return names


def _default_zone() -> str:
    from . import operation
    try:
        return operation.load().planning.timezone or "UTC"
    except Exception:          # a broken operation file is not this command's to report
        return "UTC"


def register_functions(conn: sqlite3.Connection) -> None:
    from . import job_times
    types = _type_names()
    characters = _character_names()
    zones: dict[str | None, object] = {}

    def zone(name):
        key = name or None
        if key not in zones:
            zones[key] = job_times.zone(name or _default_zone())
        return zones[key]

    def type_name(type_id):
        if type_id is None:
            return None
        try:
            ident = int(type_id)
        except (TypeError, ValueError):
            return None
        return types.get(ident, f"type {ident}")

    def character_name(ident):
        if ident is None:
            return None
        try:
            return characters.get(int(ident), str(int(ident)))
        except (TypeError, ValueError):
            return str(ident)

    def local_time(stamp, zone_name=None):
        if not stamp:
            return None
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(zone(zone_name)).strftime("%Y-%m-%d %H:%M")

    conn.create_function("type_name", 1, type_name, deterministic=True)
    conn.create_function("character_name", 1, character_name, deterministic=True)
    conn.create_function("local_time", 1, local_time, deterministic=True)
    conn.create_function("local_time", 2, local_time, deterministic=True)


def run(sql: str, limit: int = DEFAULT_LIMIT) -> tuple[list[str], list[tuple], bool]:
    """(columns, rows, cut): at most `limit` rows, `cut` when there were more."""
    if limit < 1:
        raise RuntimeError("--limit must be at least 1")
    statement = check_statement(sql)
    with contextlib.closing(connect()) as conn:
        register_functions(conn)
        try:
            cursor = conn.execute(statement)
        except sqlite3.Error as err:
            raise RuntimeError(f"query failed: {err}") from None
        columns = [d[0] for d in cursor.description or []]
        rows = [tuple(row) for row in cursor.fetchmany(limit + 1)]
    return columns, rows[:limit], len(rows) > limit


def schema(table: str | None = None) -> dict[str, list[dict]]:
    """{table: [{name, type, notnull, pk}]} straight from the database file."""
    with contextlib.closing(connect()) as conn:
        names = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if table is not None:
            if table not in names:
                raise RuntimeError(f"no table '{table}' in the ledger; tables: {', '.join(names)}")
            names = [table]
        return {name: [{"name": col[1], "type": col[2], "notnull": bool(col[3]), "pk": bool(col[5])}
                       for col in conn.execute(f'PRAGMA table_info("{name}")')] for name in names}


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}" if abs(value) >= 1000 else f"{value:g}"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return str(value)


def cmd_sql(args) -> int:
    columns, rows, cut = run(args.query, args.limit)
    note = f"cut at {args.limit} rows; pass --limit N for more" if cut else None
    if args.json:
        print(json.dumps({"columns": columns, "rows": [dict(zip(columns, row)) for row in rows],
                          "count": len(rows), "cut": cut, "limit": args.limit}, indent=2, default=str))
        return 0
    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([["" if v is None else v for v in row] for row in rows])
        if note:
            print(note, file=sys.stderr)
        return 0
    text = render.table(columns, [[_cell(v) for v in row] for row in rows]) if rows else "(no rows)"
    print(text + (f"\n{len(rows)} rows; {note}" if note else ""))
    return 0


def cmd_schema(args) -> int:
    tables = schema(args.table)
    if args.json:
        print(json.dumps(tables, indent=2))
        return 0
    blocks = []
    for name, columns in tables.items():
        rows = [[c["name"], c["type"] or "-", "pk" if c["pk"] else ("not null" if c["notnull"] else "")]
                for c in columns]
        blocks.append(f"{name}\n{render.table(['column', 'type', ''], rows)}")
    print("\n\n".join(blocks))
    return 0
