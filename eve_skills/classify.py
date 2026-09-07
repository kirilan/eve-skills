"""Alpha/omega classification of skills and clone state, based on the official
cloneGrades alpha caps (per faction) plus ESI active-vs-trained skill levels.

ESI has no direct "is omega" endpoint (confirmed: no such route or scope exists).
Evidence rules, strongest first:
  - ALPHA: a live clamp in the raw ESI data, active_skill_level < trained_skill_level.
    Pending queue completions are excluded (they legitimately leave the active level
    below the effective trained level until the character logs in). Caveat: expert
    systems can also lower active levels on omega characters.
  - OMEGA: an unclamped skill trained beyond its alpha cap, a catalog-known skill
    absent from the faction alpha list, or the *currently training* queue entry
    targeting beyond-cap levels (alpha clones cannot train past their caps).
  - LIKELY_OMEGA: only future queue entries exceed alpha caps - omega when the
    queue was built; the state may have changed since.
  - CONFLICT: live clamp and current omega evidence coexist - ESI data is minutes
    stale or the clone state just changed.
  - UNKNOWN: everything within alpha limits, or local data cannot confirm a skill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .render import parse_ts

RACE_NAMES = {1: "Caldari", 2: "Minmatar", 4: "Amarr", 8: "Gallente", 16: "Jove", 135: "Triglavian"}


def race_name(race_id: int | None) -> str:
    return RACE_NAMES.get(race_id or 0, f"race {race_id}")


def alpha_caps(data: dict, bloodline_id: int | None) -> tuple[dict[int, int], str]:
    """Return {skill_type_id: max_alpha_level} and a description of the grade used."""
    grades = data["grades"]["grades"]
    race_id = data["races"]["races"].get(str(bloodline_id)) if bloodline_id else None
    grade = grades.get(str(race_id)) if race_id else None
    if grade:
        return {int(k): v for k, v in grade["caps"].items()}, grade["name"]
    # Jove/Triglavian (and any future) races have no alpha grade of their own:
    # fall back to the union of all four faction grades with the highest cap.
    merged: dict[int, int] = {}
    for g in grades.values():
        for skill_id, cap in g["caps"].items():
            merged[int(skill_id)] = max(merged.get(int(skill_id), 0), cap)
    return merged, "union of all alpha clone grades"


@dataclass
class SkillRow:
    skill_id: int
    name: str
    trained: int              # effective level incl. pending queue completions
    active: int
    sp: int
    cap: int | None           # alpha level cap; None = absent from the alpha list
    omega_now: bool           # current-state omega evidence (unclamped beyond-cap training)
    restricted: bool          # live clamp in raw ESI data (active < raw trained)
    pending_completion: bool  # queue finished but ESI skills list not yet updated
    unknown_data: bool        # beyond alpha per ESI, but missing from local data

    @property
    def beyond_alpha(self) -> bool:
        """Trained level exceeds what the current alpha clone grade allows."""
        return self.cap is None or self.trained > self.cap


def classify_skills(skills: list[dict], caps: dict[int, int], names: dict[int, str],
                    completed_levels: dict[int, int] | None = None,
                    known_ids: set[int] | None = None) -> list[SkillRow]:
    """skills: entries from GET /characters/{id}/skills (already SP-sorted or raw).

    known_ids: skill type ids confirmed to exist in the ESI catalog (their names
    resolved). An id missing from both the alpha list and this set is reported as
    unknown local data, not as proof of omega-only.
    """
    completed_levels = completed_levels or {}
    rows = []
    for entry in skills:
        sid = int(entry["skill_id"])
        raw_trained = int(entry["trained_skill_level"])
        completed = completed_levels.get(sid, 0)
        trained = max(raw_trained, completed)
        active = int(entry["active_skill_level"])
        cap = caps.get(sid)
        # Live clamp evidence uses RAW ESI values only; a pending completion
        # explains a lagging active level without implying restriction.
        restricted = completed == 0 and active < raw_trained
        omega_now = False
        unknown_data = False
        if trained > 0 and not restricted:
            if cap is None:
                if known_ids is None or sid in known_ids:
                    omega_now = True
                else:
                    unknown_data = True
            elif trained > cap:
                omega_now = True
        rows.append(
            SkillRow(
                skill_id=sid,
                name=names.get(sid, f"skill {sid}"),
                trained=trained,
                active=min(active, trained),
                sp=int(entry["skillpoints_in_skill"]),
                cap=cap,
                omega_now=omega_now,
                restricted=restricted,
                pending_completion=sid in completed_levels and trained > raw_trained,
                unknown_data=unknown_data,
            )
        )
    return rows


@dataclass
class CloneState:
    state: str          # "ALPHA" | "OMEGA" | "LIKELY_OMEGA" | "CONFLICT" | "UNKNOWN"
    confidence: str     # "high" | "medium" | "low"
    evidence: list[str]
    warnings: list[str] = field(default_factory=list)


def clone_state(rows: list[SkillRow], queue_items: list[dict], caps: dict[int, int],
                names: dict[int, str], now: datetime | None = None,
                known_ids: set[int] | None = None,
                extra_warnings=()) -> CloneState:
    """queue_items: entries not yet finished (finish_date > now)."""
    if now is None:
        now = datetime.now(timezone.utc)

    alpha_ev = [f"{r.name} trained to {r.trained} but active at {r.active}" for r in rows if r.restricted]
    omega_ev = [
        f"{r.name} trained to level {r.trained} (alpha cap {'none - omega-only skill' if r.cap is None else r.cap})"
        for r in rows if r.omega_now
    ]
    active_queue_ev: list[str] = []
    future_queue_ev: list[str] = []
    blocked_warn: list[str] = []
    unknown_names: set[str] = {r.name for r in rows if r.unknown_data}
    for item in queue_items:
        sid = int(item["skill_id"])
        target = int(item.get("finished_level") or 0)
        cap = caps.get(sid)
        if not item.get("start_date"):
            blocked_warn.append(f"{names.get(sid, f'skill {sid}')} to L{target} has no training schedule - CCP will not train it right now")
        if cap is None and known_ids is not None and sid not in known_ids:
            unknown_names.add(names.get(sid, f"skill {sid}"))
            continue
        if not (cap is None or target > cap):
            continue
        cap_txt = "an omega-only skill" if cap is None else f"alpha cap {cap}"
        # No start date = CCP has not scheduled training (blocked or just queued).
        if item.get("start_date") and parse_ts(item["start_date"]) <= now:
            active_queue_ev.append(f"{names.get(sid, f'skill {sid}')} to L{target} in training beyond {cap_txt}")
        else:
            future_queue_ev.append(f"{names.get(sid, f'skill {sid}')} to L{target} queued beyond {cap_txt}")

    warnings = list(extra_warnings) + blocked_warn
    if unknown_names:
        warnings.append(
            "skills missing from local alpha data - it may be stale, no omega claim made for: "
            + ", ".join(sorted(unknown_names)) + " (run: eve-skills update-data)"
        )

    if alpha_ev and (omega_ev or active_queue_ev):
        return CloneState(
            "CONFLICT", "medium",
            omega_ev + active_queue_ev + [f"live clamp also present: {alpha_ev[0]}"],
            warnings + ["clamp and omega evidence coexist: ESI skill data can be minutes stale after a clone-state change"],
        )
    if alpha_ev:
        return CloneState(
            "ALPHA", "high", alpha_ev,
            warnings + ["caveat: expert systems can also lower active levels on omega characters"],
        )
    if omega_ev or active_queue_ev:
        return CloneState("OMEGA", "high", omega_ev + active_queue_ev, warnings)
    if future_queue_ev:
        return CloneState(
            "LIKELY_OMEGA", "medium", future_queue_ev,
            warnings + ["queue was built while omega; future entries alone cannot prove the current state"],
        )
    return CloneState(
        "UNKNOWN", "low",
        ["every trained and queued skill is within alpha limits - ESI exposes no direct clone-state flag"],
        warnings,
    )
