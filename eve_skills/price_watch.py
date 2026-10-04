"""Price tracking for the goods we sell: is today's Jita price a temporary low worth waiting out?

Two sources, kept in the ledger (schema v3) because ESI forgets both:

- ESI's daily regional history (`/markets/{region}/history`), about 13 months per type, one day behind.
  It gives each type's normal level and spread, and a year of past dips to learn from - so the signal
  works from the first sync instead of after weeks of polling.
- The station book, polled (`prices sync`, hourly from a timer). The daily `average` mixes fills of
  sell orders with fills of buy orders: somebody dumping into the bids drags it down while the asks we
  compete with do not move. "What would we list at now" is therefore read from the book. Comparing
  that ask with a normal made of daily averages is fair for our goods: on 2026-10-04 the lowest Jita
  ask of the 25 watched types sat a median 0.5% from their last week's average - most fills lift asks.

The rule is deliberately plain and robust rather than a forecast. Normal is the median daily average of
the last BASELINE_DAYS; spread is the median absolute deviation scaled to a standard deviation (floored
at MIN_SCALE_PCT, since some types trade at one price for days). A price DIP_Z spreads under normal is
a *dip* - unless the last SHORT_DAYS have been that low too, which is a new level (*falling*), and
waiting for a recovery that is not coming only costs time. A dip is held until the price is back near
normal, or HORIZON_DAYS at most, and never more than HORIZON_DAYS of our own sales.

What the rule is worth was measured on 2026-10-04 over a year of Jita history for the 15 T2 rigs and
modules we sell (`prices backtest`), selling each held unit on the first day back near normal or at the
horizon: dips paid a median +2.3% per held unit (mean +5.0%, 56% of holds gained); "falling" starts
-1.2% (49%) and mild lows (1-2 spreads under) +0.4% - which is why only dips are held. Gating a dip on
how the same type's earlier dips went was tried and made it worse (median -0.3%): for these lines one
dip does not predict the next, so a type's own record is shown as context, never as a condition.
The edge is small and noisy, and it is measured on the daily average, not on the asks we list against.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from . import esi as esi_mod, ledger_db, market

HUB = market.HUBS["jita"]

BASELINE_DAYS = 30      # what "normal" is measured over
SHORT_DAYS = 7          # a low lasting this long is a new level, not a dip
MIN_ROWS = 15           # days with trades the baseline needs before it says anything
MAD_TO_SIGMA = 1.4826   # median absolute deviation -> standard deviation, for normally spread prices
MIN_SCALE_PCT = 1.0     # spread floor, % of the baseline: a flat week must not make 0.5% a "dip"
DIP_Z = 2.0             # this many spreads under normal is low ...
HIGH_Z = 2.0            # ... and this many over is high
RECOVER_Z = 0.5         # back within this many spreads of normal counts as recovered
HORIZON_DAYS = 14       # how long a dip may be waited out (two weeks of stock, decided 2026-10-04)

# The automatic watchlist: what we sold at least this often lately, and what our jobs made.
WATCH_SALES_DAYS = 60
WATCH_MIN_SALES = 3
MANUFACTURING = 1

SNAPSHOT_MAX_AGE_HOURS = 3   # an older snapshot is not "now"; the signal falls back to history
# ESI publishes yesterday's history late in the day (one document per type, ~400 days long), so a type
# whose newest day is still behind is re-read at most this often rather than on every hourly poll.
HISTORY_RETRY_HOURS = 4


@dataclass(frozen=True)
class Day:
    day: date
    average: float
    highest: float | None
    lowest: float | None
    volume: int


def days_from_rows(rows: Iterable[Mapping]) -> list[Day]:
    """Stored or ESI history rows as Days, oldest first; rows without a price are skipped."""
    out = []
    for r in rows:
        stamp = r.get("day") or r.get("date")
        if not stamp or r.get("average") is None:
            continue
        out.append(Day(date.fromisoformat(str(stamp)[:10]), float(r["average"]),
                       None if r.get("highest") is None else float(r["highest"]),
                       None if r.get("lowest") is None else float(r["lowest"]),
                       int(r.get("volume") or 0)))
    return sorted(out, key=lambda d: d.day)


@dataclass(frozen=True)
class Baseline:
    end: date              # last day included
    median: float          # normal price
    scale: float           # one spread, ISK
    short_median: float    # the last SHORT_DAYS
    rows: int              # days with trades in the window
    volume_per_day: float  # calendar days, like market.history_stats

    @property
    def short_z(self) -> float:
        return (self.short_median - self.median) / self.scale


def baseline(days: Sequence[Day], end: date, window: int = BASELINE_DAYS) -> Baseline | None:
    """Normal price and spread over the `window` calendar days ending on `end`; None when too thin."""
    start = end - timedelta(days=window - 1)
    picked = [d for d in days if start <= d.day <= end]
    if len(picked) < MIN_ROWS:
        return None
    prices = [d.average for d in picked]
    median = statistics.median(prices)
    mad = statistics.median(abs(p - median) for p in prices)
    scale = max(MAD_TO_SIGMA * mad, median * MIN_SCALE_PCT / 100.0)
    short_start = end - timedelta(days=SHORT_DAYS - 1)
    short = [d.average for d in picked if d.day >= short_start] or prices[-1:]
    return Baseline(end=end, median=median, scale=scale, short_median=statistics.median(short),
                    rows=len(picked), volume_per_day=sum(d.volume for d in picked) / window)


# What a price is, against its baseline.
HIGH, NORMAL, DIP, FALLING = "high", "normal", "dip", "falling"


def classify(price: float, base: Baseline) -> tuple[str, float]:
    """(state, z): how many spreads `price` sits from normal, and what that makes it."""
    z = (price - base.median) / base.scale
    if z >= HIGH_Z:
        return HIGH, z
    if z <= -DIP_Z:
        return (FALLING if base.short_z <= -DIP_Z else DIP), z
    return NORMAL, z


@dataclass(frozen=True)
class Episode:
    """One past dip: when it started, how deep, how many days until the price came back, and what a
    unit held from the first dip day fetched when sold - on the recovery day, else the last day of the
    horizon - against selling it that first day."""
    start: date
    state: str                    # dip | falling: falling episodes are kept to measure the rule
    depth_pct: float
    recovered_after: int | None   # None: not within the horizon (or the history ends first)
    censored: bool                # the history ended before the horizon did
    hold_return_pct: float | None  # None when no later day traded


def dip_episodes(days: Sequence[Day], horizon: int = HORIZON_DAYS) -> list[Episode]:
    """Every past dip and falling start by the live rule, each judged only by the days before it (no
    look-ahead).

    Consecutive days in one state are one episode: the first day is when a seller would have had to
    decide. Recovered means a later daily average back within RECOVER_Z spreads of the baseline the
    dip was measured against."""
    out: list[Episode] = []
    previous = None
    for i, today in enumerate(days):
        base = baseline(days[:i], today.day - timedelta(days=1))
        if base is None:
            continue
        state, _z = classify(today.average, base)
        if state not in (DIP, FALLING) or state == previous:
            previous = state
            continue
        previous = state
        target = base.median - RECOVER_Z * base.scale
        last = today.day + timedelta(days=horizon)
        after = sold = None
        for later in days[i + 1:]:
            if later.day > last:
                break
            sold = later.average
            if later.average >= target:
                after = (later.day - today.day).days
                break
        censored = after is None and days[-1].day < last
        out.append(Episode(today.day, state, (today.average / base.median - 1) * 100, after, censored,
                           None if sold is None else (sold / today.average - 1) * 100))
    return out


@dataclass(frozen=True)
class Evidence:
    """How one state's past episodes went: context for a person, never a condition of the rule."""
    dips: int                  # past episodes whose horizon has passed
    recovered: int
    median_days: float | None
    median_return_pct: float | None   # per held unit, see Episode
    mean_return_pct: float | None
    wins: int                  # holds that fetched more than selling on the first day

    @property
    def rate(self) -> float | None:
        return self.recovered / self.dips if self.dips else None


def evidence(episodes: Sequence[Episode], state: str = DIP) -> Evidence:
    done = [e for e in episodes if e.state == state and not e.censored]
    times = [e.recovered_after for e in done if e.recovered_after is not None]
    returns = [e.hold_return_pct for e in done if e.hold_return_pct is not None]
    return Evidence(len(done), len(times), statistics.median(times) if times else None,
                    statistics.median(returns) if returns else None,
                    statistics.mean(returns) if returns else None, sum(1 for r in returns if r > 0))


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Backtest:
    days: int               # days simulated
    dip_days: int           # days the rule said "hold"
    sell_daily: float       # average price per unit, selling every unit the day it arrives
    hold_dips: float        # average price per unit, holding through dips
    held_unit_days: int     # capital tied up: units x days held
    forced: int             # units sold at the horizon, still in a dip

    @property
    def uplift_pct(self) -> float:
        return (self.hold_dips / self.sell_daily - 1) * 100 if self.sell_daily else 0.0


def backtest(days: Sequence[Day], horizon: int = HORIZON_DAYS, cap_days: int = HORIZON_DAYS,
             proxy: str = "average") -> Backtest | None:
    """One unit arrives every day. *Sell daily* sells it at that day's price; *hold dips* keeps it while
    the rule says dip (no more than `cap_days` units, none longer than `horizon`) and sells everything
    held on the first day that is not. The price a unit fetches is that day's `proxy` (`average`, or
    `highest` - the top of the day's fills, closer to the asks). Prices are per unit, before fees: fees
    are a percentage, so they move both strategies alike."""
    def price(d: Day) -> float:
        return d.highest if proxy == "highest" and d.highest is not None else d.average

    sim = []
    for i, today in enumerate(days):
        base = baseline(days[:i], today.day - timedelta(days=1))
        if base is not None:
            sim.append((today, classify(today.average, base)[0]))
    if not sim:
        return None
    daily = sum(price(d) for d, _s in sim) / len(sim)
    held: list[date] = []
    revenue = 0.0
    unit_days = forced = dip_days = 0
    for today, state in sim:
        held.append(today.day)
        if state == DIP:
            dip_days += 1
        expired = [a for a in held if (today.day - a).days >= horizon]
        if state == DIP:
            forced += len(expired)
            keep = [a for a in held if (today.day - a).days < horizon]
            sell_now = len(held) - len(keep)
            # Over the cap: the oldest units go first, the newest wait.
            while len(keep) > cap_days:
                keep.pop(0)
                sell_now += 1
        else:
            keep, sell_now = [], len(held)
        revenue += sell_now * price(today)
        held = keep
        unit_days += len(held)
    revenue += len(held) * price(sim[-1][0])
    return Backtest(len(sim), dip_days, daily, revenue / len(sim), unit_days, forced)


# ---------------------------------------------------------------------------
# the book: one station's orders reduced to what a seller compares against
# ---------------------------------------------------------------------------

def reduce_book(rows: Sequence[Mapping], location_id: int, type_id: int, ts: str, polled_at: str,
                our_orders: set[int]) -> dict:
    """One poll of a station's book as a `book_snapshots` row. The ask side is the competition: our own
    orders are left out of `best_sell` and the depth figures (counted apart in `our_*`), or our own
    listing would be read back as "the market". Units within 1% and 5% of the lowest competing ask say
    whether the floor is one small order (gone within hours) or a wall."""
    here = [r for r in rows if int(r.get("location_id") or 0) == location_id]
    sells = sorted(((float(r["price"]), int(r.get("volume_remain") or 0), int(r.get("order_id") or 0))
                    for r in here if not r.get("is_buy_order") and r.get("price") is not None))
    buys = [(float(r["price"]), int(r.get("volume_remain") or 0))
            for r in here if r.get("is_buy_order") and r.get("price") is not None]
    ours = [(p, v) for p, v, oid in sells if oid in our_orders]
    sells = [row for row in sells if row[2] not in our_orders]
    best_sell = sells[0][0] if sells else None

    def within(pct: float) -> int:
        return 0 if best_sell is None else sum(v for p, v, _o in sells if p <= best_sell * (1 + pct / 100))

    return {"location_id": location_id, "type_id": type_id, "ts": ts, "polled_at": polled_at,
            "best_sell": best_sell, "best_buy": max((p for p, _v in buys), default=None),
            "sell_units": sum(v for _p, v, _o in sells), "sell_orders": len(sells),
            "buy_units": sum(v for _p, v in buys), "buy_orders": len(buys),
            "sell_units_1pct": within(1), "sell_units_5pct": within(5),
            "our_sell_units": sum(v for _p, v in ours), "our_best_sell": min((p for p, _v in ours), default=None)}


# ---------------------------------------------------------------------------
# watchlist and sync
# ---------------------------------------------------------------------------

def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def watchlist(conn: sqlite3.Connection, now: datetime) -> dict[int, str]:
    """type id -> why it is watched ('sales', 'jobs' or 'manual'). Automatic: sold at least
    WATCH_MIN_SALES times in the last WATCH_SALES_DAYS, or made by one of our manufacturing jobs in
    that time. Manual additions and exclusions win over the rule."""
    since = _iso(now - timedelta(days=WATCH_SALES_DAYS))
    out: dict[int, str] = {}
    for (type_id,) in conn.execute("SELECT product_type_id FROM jobs WHERE activity_id = ? AND start_date >= ? "
                                   "AND product_type_id IS NOT NULL GROUP BY product_type_id",
                                   (MANUFACTURING, since)):
        out[int(type_id)] = "jobs"
    for (type_id,) in conn.execute("SELECT type_id FROM transactions WHERE is_buy = 0 AND date >= ? "
                                   "GROUP BY type_id HAVING COUNT(*) >= ?", (since, WATCH_MIN_SALES)):
        out[int(type_id)] = "sales"
    for type_id, mode in ledger_db.watchlist_changes(conn).items():
        if mode == "exclude":
            out.pop(type_id, None)
        else:
            out[type_id] = "manual"
    return out


def our_open_sell_orders(conn: sqlite3.Connection) -> set[int]:
    return {int(r[0]) for r in conn.execute("SELECT order_id FROM orders WHERE is_buy = 0 AND state = 'open'")}


@dataclass
class SyncResult:
    types: int = 0
    history_fetched: int = 0
    history_new_days: int = 0
    snapshots_new: int = 0
    problems: list[str] | None = None

    def to_json(self) -> dict:
        return asdict(self)


def sync(conn: sqlite3.Connection, client: esi_mod.Esi, *, history: bool = True, book: bool = True,
         types: Iterable[int] | None = None) -> SyncResult:
    """Refresh history (only types whose stored history is older than yesterday) and store one Jita
    book snapshot per watched type. Never raises for one type: a failed document is a problem line,
    and the next run reads it again."""
    now = client.now()
    polled_at = _iso(now)
    wanted = sorted(types) if types is not None else sorted(watchlist(conn, now))
    result = SyncResult(types=len(wanted), problems=[])
    yesterday = (now.astimezone(timezone.utc).date() - timedelta(days=1)).isoformat()
    ours = our_open_sell_orders(conn)
    retry_after = _iso(now - timedelta(hours=HISTORY_RETRY_HOURS))
    for type_id in wanted:
        read_key = f"prices:history_read:{type_id}"
        behind = (ledger_db.newest_history_day(conn, HUB.region_id, type_id) or "") < yesterday
        if history and behind and (ledger_db.get_meta(conn, read_key) or "") <= retry_after:
            try:
                rows = client.get(f"/markets/{HUB.region_id}/history?type_id={type_id}")
            except esi_mod.EsiError as err:
                result.problems.append(f"history {type_id}: {err}")
            else:
                with conn:
                    result.history_new_days += ledger_db.upsert_market_history(conn, HUB.region_id, type_id, rows)
                    ledger_db.set_meta(conn, read_key, polled_at)
                result.history_fetched += 1
        if book:
            try:
                rows, meta = client.get_meta(market.book_path(HUB.region_id, type_id))
            except esi_mod.EsiError as err:
                result.problems.append(f"book {type_id}: {err}")
                continue
            ts = market.iso_utc(meta.last_modified) or polled_at
            with conn:
                result.snapshots_new += ledger_db.insert_book_snapshot(
                    conn, reduce_book(rows, HUB.station_id, type_id, ts, polled_at, ours))
    with conn:
        ledger_db.log_sync(conn, "prices", polled_at, result.types, result.history_new_days + result.snapshots_new,
                           None, None)
    return result


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stock:
    qty: float               # units the ledger holds (hangars and open sell orders)
    unit_cost: float | None
    sold_per_day: float      # our own sales, last 30 days, every hub


SALES_RATE_DAYS = 30


def stock_figures(conn: sqlite3.Connection, pools: Mapping, now: datetime, type_ids: Iterable[int]) -> dict[int, Stock]:
    """Per type: the ledger's stock and unit cost (`ledger.Book.pools`) and our own sales rate."""
    since = _iso(now - timedelta(days=SALES_RATE_DAYS))
    sold = {int(r[0]): float(r[1]) for r in conn.execute(
        "SELECT type_id, SUM(quantity) FROM transactions WHERE is_buy = 0 AND date >= ? GROUP BY type_id", (since,))}
    out = {}
    for type_id in type_ids:
        pool = pools.get(("item", type_id))
        out[type_id] = Stock(qty=pool.qty if pool else 0.0, unit_cost=pool.unit if pool else None,
                             sold_per_day=sold.get(type_id, 0.0) / SALES_RATE_DAYS)
    return out


def realised_fee_pct(entries: Iterable, now: datetime, days: int = SALES_RATE_DAYS) -> float | None:
    """Sales tax plus broker fees as a % of revenue over the last `days`, from the ledger's own P&L
    lines - what selling actually cost us, rather than a rate computed from skills and standings."""
    since = _iso(now - timedelta(days=days))
    revenue = fees = 0.0
    for e in entries:
        if e.date < since or e.scope == "overhead":
            continue
        if e.kind == "revenue":
            revenue += e.amount
        elif e.kind in ("sales_tax", "broker_fee"):
            fees -= e.amount
    return fees / revenue * 100 if revenue > 0 else None


SELL, HOLD = "sell", "hold"


@dataclass(frozen=True)
class Advice:
    type_id: int
    state: str | None            # high | normal | dip | falling; None without a baseline
    price: float | None          # "now": the book's lowest ask, or the newest daily average
    price_source: str            # 'book <ts>' | 'history <day>' | 'none'
    base: Baseline | None
    z: float | None
    evidence: Evidence
    stock: Stock
    hold_cap: float | None       # units we may hold: HORIZON_DAYS of our own sales
    hold_qty: float
    sell_qty: float
    target: float | None         # where to list held stock: back at normal
    net_now: float | None        # per unit after fees, at `price`
    action: str
    reason: str


def advise(type_id: int, days: Sequence[Day], snapshot: Mapping | None, stock: Stock,
           fee_pct: float | None, now: datetime) -> Advice:
    """Sell or hold one type's stock, and why. Only a dip is ever held - high, normal and falling all
    sell - and never more than HORIZON_DAYS of our own sales. With no sales rate yet the whole stock
    may be held, and the reason says so."""
    episodes = dip_episodes(days)
    ev = evidence(episodes)
    price, source = None, "none"
    if snapshot and snapshot.get("best_sell") is not None:
        stamp = datetime.fromisoformat(str(snapshot["ts"]).replace("Z", "+00:00"))
        if (now - stamp).total_seconds() <= SNAPSHOT_MAX_AGE_HOURS * 3600:
            price, source = float(snapshot["best_sell"]), f"book {snapshot['ts']}"
    newest = days[-1] if days else None
    base = None
    if newest is not None:
        # Live price: normal is everything up to the newest day. History price: everything before it.
        base = baseline(days, newest.day if price is not None else newest.day - timedelta(days=1))
        if price is None:
            price, source = newest.average, f"history {newest.day.isoformat()}"
    cap = HORIZON_DAYS * stock.sold_per_day if stock.sold_per_day > 0 else None
    net = None if price is None or fee_pct is None else price * (1 - fee_pct / 100)
    if base is None or price is None:
        return Advice(type_id, None, price, source, base, None, ev, stock, cap, 0.0, stock.qty, None, net,
                      SELL, "no baseline yet: fewer than %d trading days in the last %d" % (MIN_ROWS, BASELINE_DAYS))
    state, z = classify(price, base)
    target = round(base.median, 2)
    hold = 0.0
    if state == HIGH:
        reason = "above normal - list now"
    elif state == NORMAL:
        reason = "normal price"
    elif state == FALLING:
        reason = "low for a week already: a new level, waiting is unlikely to pay"
    else:
        hold = stock.qty if cap is None else min(stock.qty, cap)
        past = (f"its past dips: {ev.recovered}/{ev.dips} back to normal, median hold "
                f"{ev.median_return_pct:+.1f}%" if ev.dips and ev.median_return_pct is not None
                else "no past dips of its own")
        # One listing per item per hub (user rule, 2026-10-04): a part sold now and a part listed at
        # normal would be two orders, so the held part waits in the hangar - unless all of it is held,
        # and then the whole stack may go up once at normal and fill when the price comes back.
        how = ((f"nothing held - for new stock keep up to {cap:,.0f} in the hangar and list the rest"
                if cap is not None else "nothing held") if stock.qty <= 0
               else f"keep it in the hangar, or list the whole stack once at {target:,.2f}" if hold >= stock.qty
               else f"list {stock.qty - hold:,.0f} now, keep {hold:,.0f} in the hangar until that order sells out")
        reason = (f"dip - {how} ({past})" + ("; no sales rate yet, so no hold cap" if cap is None else ""))
    action = HOLD if hold > 0 else SELL
    return Advice(type_id, state, price, source, base, z, ev, stock, cap, hold, stock.qty - hold,
                  target if state == DIP else None, net, action, reason)
