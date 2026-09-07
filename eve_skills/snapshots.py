"""Local SP history: one JSONL row appended per character per successful run.

Writers serialise on ``sp-history.lock``; readers need none, since every write lands
as an atomic replace and is therefore always seen whole."""

from __future__ import annotations

import json
import os
import time

from . import paths, storage


def history_file(create: bool = True) -> str:
    """The SP-history JSONL; create=False resolves the path without touching disk."""
    return os.path.join(paths.config_dir(create=create), "sp-history.jsonl")


RETENTION_DAYS = 60  # generous over the 7-day consumers; keeps watch-mode history from growing forever


def _recent(line: str, cutoff_ts: float) -> bool:
    try:
        return float(json.loads(line)["ts"]) >= cutoff_ts
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False  # corrupt lines are dropped on prune; load() always skipped them


def record(character_id: int, total_sp: int):
    """Append one row, dropping anything past retention.

    The read-prune-rewrite runs under a lock: watch mode and a manual command can
    record in the same second, and each rewriting from its own stale read would leave
    only the last writer's rows on disk."""
    path = history_file()
    row = json.dumps({"ts": round(time.time()), "char_id": int(character_id), "total_sp": int(total_sp)}) + "\n"
    cutoff = time.time() - RETENTION_DAYS * 86400
    with storage.file_lock(os.path.join(paths.config_dir(), "sp-history.lock")):
        try:
            with open(path) as fh:
                kept = [ln for ln in fh if _recent(ln, cutoff)]
        except FileNotFoundError:
            kept = []
        kept.append(row)
        storage.atomic_write(path, "".join(kept))


def load() -> list[dict]:
    rows = []
    try:
        with open(history_file(create=False)) as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    rows.append({"ts": float(row["ts"]), "char_id": int(row["char_id"]), "total_sp": int(row["total_sp"])})
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue  # skip corrupt lines; history is append-only diagnostics
    except FileNotFoundError:
        pass
    return rows


def latest(character_id: int) -> dict | None:
    best = None
    for row in load():
        if row["char_id"] == character_id and (best is None or row["ts"] > best["ts"]):
            best = row
    return best


def latest_before(character_id: int, cutoff_ts: float) -> dict | None:
    """Newest row at or before cutoff — the baseline for a period delta."""
    best = None
    for row in load():
        if row["char_id"] == character_id and row["ts"] <= cutoff_ts and (best is None or row["ts"] > best["ts"]):
            best = row
    return best
