"""Human and machine representations of ESI document cache timestamps."""

from __future__ import annotations

from datetime import datetime, timezone

from . import esi as esi_mod, render


def _utc(epoch: float | None) -> datetime | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc)


def iso(epoch: float | None) -> str | None:
    stamp = _utc(epoch)
    return None if stamp is None else stamp.isoformat().replace("+00:00", "Z")


def document(meta: esi_mod.Meta) -> dict:
    """Stable machine fields for one ESI payload's generation and refresh times."""
    return {"last_modified": iso(meta.last_modified), "expires": iso(meta.expires)}


def line(label: str, meta: esi_mod.Meta) -> str:
    """One compact cache line, using the endpoint's headers rather than local fetch time."""
    modified = _utc(meta.last_modified)
    expires = _utc(meta.expires)
    if modified is None:
        return f"{label} cache time unavailable"
    text = f"{label} as of {modified.strftime('%H:%M')} UTC"
    if expires is not None:
        text += f", next refresh {expires.strftime('%H:%M')}"
    return text


def delivered_after(jobs: list[dict], meta: esi_mod.Meta) -> int:
    """Completed jobs whose delivery stamp is newer than the payload represented by meta."""
    if meta.last_modified is None:
        return 0
    snapshot = _utc(meta.last_modified)
    return sum(
        1 for job in jobs
        if job.get("status") == "delivered"
        and (completed := render.parse_opt(job.get("completed_date"))) is not None
        and completed > snapshot
    )


def delivery_warning(count: int, subject: str) -> str | None:
    if count <= 0:
        return None
    noun = "job" if count == 1 else "jobs"
    return (f"{count} {noun} delivered after the {subject} snapshot — their output is not in "
            "these numbers")
