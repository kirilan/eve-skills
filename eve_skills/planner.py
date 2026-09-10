"""Training-rate calibration, skill-level SP cost and Skill Extractor math.

Pure functions over already-fetched data; no network access here.

SP cost model (CCP): what a level is *worth* depends only on the skill's rank (the SDE
``skillTimeConstant`` multiplier); attributes never change SP, only how fast it accrues:

    cumulative SP held at level L = round(250 * rank * 2 ** (2.5 * (L - 1)))
    rank 1 -> 250 / 1,414 / 8,000 / 45,255 / 256,000   (the canonical table)

Training time is therefore SP divided by a measured SP/hour rate, which is why calibration
from a live TRAINING queue item beats any formula - the measurement already includes
implants, remaps, alpha/omega status and the attributes of the skill being trained.
Caveat: one calibrated rate is one number for a whole plan, while skills driven by other
attributes really train at other rates, so plans spanning several attribute pairs are
estimates (the caller names the attributes involved). Implants are never modeled into
future levels.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone

from . import snapshots
from .alphadata import SkillInfo
from .render import parse_ts

LEVEL_SP_BASE = 250          # cumulative SP for level 1 of a rank-1 skill
LEVEL_GROWTH = 2 ** 2.5      # each level's cumulative total is this times the previous one
MAX_LEVEL = 5

EXTRACTION = {
    "extractor_sp": 500_000,        # trained SP removed per Skill Extractor
    "min_trained_sp": 5_500_000,    # allocated (trained) SP required to extract at all
    "floor_sp": 5_000_000,          # must remain after each extraction
    # Injector SP granted depends on the RECEIVING character's total SP at injection:
    "injector_tiers": [(5_000_000, 500_000), (50_000_000, 400_000), (80_000_000, 300_000)],
    "injector_min": 150_000,        # total SP >= 80M
    "source": "https://support.eveonline.com/hc/en-us/articles/207605005-Skill-Extractors-and-Skill-Injectors",
    "verified": "2026-09-05",       # date these rules were re-checked against CCP
}
STALE_AFTER_DAYS = 180


def cumulative_sp(level: int, rank: int) -> int:
    """Total SP held in a skill of `rank` once it reaches `level` (0..5)."""
    if not 0 <= level <= MAX_LEVEL:
        raise ValueError(f"skill level must be 0..{MAX_LEVEL}, got {level}")
    if rank < 1:
        raise ValueError(f"skill rank must be a positive multiplier, got {rank}")
    return round(LEVEL_SP_BASE * rank * LEVEL_GROWTH ** (level - 1)) if level else 0


def level_sp(level: int, rank: int) -> int:
    """SP for exactly one level of a skill of `rank`."""
    return cumulative_sp(level, rank) - cumulative_sp(level - 1, rank)


def levels_sp(from_level: int, to_level: int, rank: int) -> int:
    """Total SP to go from `from_level` (0..4) up to and including `to_level`."""
    if not 0 <= from_level <= to_level <= MAX_LEVEL:
        raise ValueError(f"needs 0 <= from_level <= to_level <= {MAX_LEVEL}, got {from_level}->{to_level}")
    return cumulative_sp(to_level, rank) - cumulative_sp(from_level, rank)


class PlanError(RuntimeError):
    """The requested targets cannot be priced or ordered with the local skill catalog."""


@dataclass(frozen=True)
class PlanItem:
    """One queue entry the plan still has to add."""

    skill_id: int
    name: str
    rank: int
    trained_level: int              # what ESI reports today
    from_level: int                 # where training resumes, incl. pending completions
    to_level: int
    sp: int
    requested: bool                 # the user named this skill (vs. a pulled-in prerequisite)
    required_by: tuple[str, ...]    # needed skills this one unlocks


@dataclass(frozen=True)
class CoveredSkill:
    """A target or prerequisite already at the needed level once the existing queue drains."""

    skill_id: int
    name: str
    level: int
    target: int
    via_queue: bool                 # reached by a pending queue entry, not trained yet


@dataclass(frozen=True)
class TrainingPlan:
    """``items`` is ordered so every prerequisite precedes the skills needing it."""

    items: list[PlanItem]
    covered: list[CoveredSkill]

    @property
    def total_sp(self) -> int:
        return sum(item.sp for item in self.items)


def scheduled_levels(queue: list[dict]) -> dict[int, int]:
    """Levels the existing queue will reach, highest per skill.

    Entries CCP gave no dates to cannot start training, so they promise nothing and are
    not counted as coverage."""
    levels: dict[int, int] = {}
    for item in queue:
        if not item.get("start_date") or not item.get("finish_date"):
            continue
        sid = int(item["skill_id"])
        level = int(item.get("finished_level") or 0)
        levels[sid] = max(levels.get(sid, 0), level)
    return levels


def _name(skill_id: int, catalog: dict[int, SkillInfo]) -> str:
    info = catalog.get(skill_id)
    return info.name if info else f"skill {skill_id}"


def _require(skill_id: int, catalog: dict[int, SkillInfo], because: str | None = None) -> SkillInfo:
    info = catalog.get(skill_id)
    if info is None:
        raise PlanError(
            f"{because or 'skill ' + str(skill_id)} needs skill {skill_id}, which the local skill "
            "catalog does not know - run: eve-skills update-data"
        )
    if info.rank < 1:
        raise PlanError(f"{info.name} has no training multiplier in the local skill catalog "
                        "- it cannot be priced; run: eve-skills update-data")
    return info


def _cycle(stuck: set[int], prereqs: dict[int, set[int]]) -> list[int]:
    """One representative cycle among `stuck`, e.g. [A, B, A] for display."""
    chain: list[int] = []
    seen: dict[int, int] = {}
    node = min(stuck)
    while node not in seen:
        seen[node] = len(chain)
        chain.append(node)
        node = sorted(prereqs[node] & stuck)[0]
    return chain[seen[node]:] + [node]


def build_plan(targets: dict[int, int], catalog: dict[int, SkillInfo],
               trained: dict[int, int], scheduled: dict[int, int] | None = None) -> TrainingPlan:
    """Expand `targets` {skill_id: desired level} into an ordered, priced training list.

    Prerequisites are pulled in recursively at the highest level any target needs them, so
    a shared prerequisite appears exactly once; every skill is charged only for the levels
    it does not already have. `trained` is the character's current levels and `scheduled`
    those an existing queue entry will reach - new items start after the queue drains, so
    scheduled levels are not charged either. Raises PlanError on an unknown skill, a
    prerequisite the catalog has no row for, or a cyclic prerequisite chain."""
    scheduled = scheduled or {}
    needed: dict[int, int] = {}
    dependents: dict[int, set[int]] = defaultdict(set)
    frontier = deque(sorted(targets.items()))
    while frontier:
        sid, level = frontier.popleft()
        if not 1 <= level <= MAX_LEVEL:
            raise PlanError(f"{_name(sid, catalog)} target level {level} is outside 1..{MAX_LEVEL}")
        if level <= needed.get(sid, 0):
            continue    # an equal or deeper requirement already expanded this skill
        info = _require(sid, catalog)
        needed[sid] = level
        for pre_id, pre_level in sorted(info.prerequisites.items()):
            _require(pre_id, catalog, because=info.name)
            dependents[pre_id].add(sid)
            frontier.append((pre_id, pre_level))

    # Kahn's algorithm over the needed subgraph: prerequisites before dependents, ties by
    # depth then name so the order is stable and foundations come first.
    prereqs = {sid: {p for p in catalog[sid].prerequisites if p in needed} for sid in needed}
    waiters = defaultdict(list)
    for sid, pres in prereqs.items():
        for pre in pres:
            waiters[pre].append(sid)
    blocking = {sid: len(pres) for sid, pres in prereqs.items()}
    depth: dict[int, int] = {}
    order: list[int] = []
    ready = [sid for sid, count in blocking.items() if count == 0]
    while ready:
        ready.sort(key=lambda s: (depth.get(s, 0), catalog[s].name.lower()))
        sid = ready.pop(0)
        order.append(sid)
        for waiter in waiters.get(sid, ()):
            blocking[waiter] -= 1
            depth[waiter] = max(depth.get(waiter, 0), depth.get(sid, 0) + 1)
            if blocking[waiter] == 0:
                ready.append(waiter)
    if len(order) != len(needed):
        stuck = {sid for sid in needed if sid not in set(order)}
        chain = " -> ".join(catalog[s].name for s in _cycle(stuck, prereqs))
        raise PlanError(f"cyclic prerequisites in the skill catalog: {chain}")

    items: list[PlanItem] = []
    covered: list[CoveredSkill] = []
    for sid in order:
        info = catalog[sid]
        to_level = needed[sid]
        from_level = max(trained.get(sid, 0), scheduled.get(sid, 0))
        if to_level <= from_level:
            covered.append(CoveredSkill(sid, info.name, from_level, to_level,
                                        via_queue=scheduled.get(sid, 0) > trained.get(sid, 0)))
            continue
        items.append(PlanItem(
            skill_id=sid, name=info.name, rank=info.rank, trained_level=trained.get(sid, 0),
            from_level=from_level, to_level=to_level, sp=levels_sp(from_level, to_level, info.rank),
            requested=sid in targets, required_by=tuple(sorted(_name(d, catalog) for d in dependents.get(sid, ()))),
        ))
    return TrainingPlan(items=items, covered=covered)


def snapshot_rate(ctx, window_days: float = 7.0) -> float | None:
    """SP/hour from local SP history over the recent window; None when too sparse."""
    char_id = ctx["token"]["character_id"]
    now_ts = ctx["now"].timestamp()
    rows = [r for r in snapshots.load() if r["char_id"] == char_id and r["ts"] >= now_ts - window_days * 86400]
    if len(rows) < 2:
        return None
    first = min(rows, key=lambda r: r["ts"])
    last = max(rows, key=lambda r: r["ts"])
    hours = (last["ts"] - first["ts"]) / 3600
    if hours < 1:
        return None
    gain = last["total_sp"] - first["total_sp"]
    if gain <= 0:
        return None  # flat or extraction-dipped history is not a measurable rate; callers must ask for --rate
    return gain / hours


def calibrated_rate(ctx) -> tuple[float, str]:
    """(sp_per_hour, source). Live TRAINING item is ground truth; snapshot slope is the fallback."""
    now = ctx["now"]
    for item in ctx["queue"]:
        if not item.get("start_date") or not item.get("finish_date"):
            continue
        start, finish = parse_ts(item["start_date"]), parse_ts(item["finish_date"])
        if not (start <= now < finish):
            continue
        span_h = (finish - start).total_seconds() / 3600
        # `start_date` is not where the level began: EVE restamps it on the active item every time
        # the queue is rearranged, so pairing it with `level_start_sp` measures a whole level's SP
        # against the few hours since the last queue edit and reports a rate several times too fast.
        # `training_start_sp` is the SP held at that `start_date` - the only figure this span
        # describes. An item that omits it cannot be measured here at all: defaulting to 0 would
        # read the entire level as gained inside the span, so the item is skipped and the SP
        # history gets the question instead. Zero is a real value (a level started from scratch),
        # which is why this tests for absence rather than falsiness.
        if item.get("training_start_sp") is None:
            continue
        gain = int(item.get("level_end_sp") or 0) - int(item["training_start_sp"])
        if span_h > 0 and gain > 0:
            return gain / span_h, "live training item"
    rate = snapshot_rate(ctx)
    if rate is not None:
        return rate, "local SP history (7d)"
    raise RuntimeError(
        "cannot estimate training rate: nothing is training right now and local SP history is too short. "
        "Pass --rate <sp-per-hour> or let the character train for a while first."
    )


def injector_value(total_sp_at_injection: int) -> int:
    for cap, value in EXTRACTION["injector_tiers"]:
        if total_sp_at_injection < cap:
            return value
    return EXTRACTION["injector_min"]


def extraction_rules_warning(now: datetime | None = None) -> str | None:
    """Staleness notice when the dated CCP rules may no longer be accurate."""
    verified = datetime.strptime(EXTRACTION["verified"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    age = ((now or datetime.now(timezone.utc)) - verified).days
    if age > STALE_AFTER_DAYS:
        return (f"extraction rules last verified {EXTRACTION['verified']} ({age} days ago) - "
                "check CCP before relying on them")
    return None


def extraction_plan(allocated_sp: int, total_sp: int, sp_per_hour: float | None) -> dict:
    """How many extractors the character can run and what re-training them costs."""
    e = EXTRACTION
    out: dict = {"count": 0, "reason": "", "retrain_days_each": None, "injector_value": injector_value(total_sp)}
    if allocated_sp < e["min_trained_sp"]:
        out["reason"] = (f"needs at least {e['min_trained_sp']:,} trained (allocated) SP to extract; "
                         f"character has {allocated_sp:,}")
        return out
    out["count"] = (allocated_sp - e["floor_sp"]) // e["extractor_sp"]
    if sp_per_hour:
        out["retrain_days_each"] = e["extractor_sp"] / sp_per_hour / 24
    return out
