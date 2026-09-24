"""The accounting engine: what the stored ESI facts cost, earned and are worth.

Pure domain logic over the rows `ledger_db` keeps - no ESI, no printing. Every report replays the
whole history in time order, so a better rule re-prices the past instead of only what follows it.

The model, stated once:

* **Stock is costed at moving weighted average.** Every item type, and every blueprint type's copy
  runs, is a pool of (quantity, ISK). A purchase adds its price; a broker fee on a buy order adds
  its ISK to that type's pool; a finished job adds its whole cost as the value of what it made.
  Consumption and sales take the pool's average.
* **Stock the ledger never saw arrive is valued at market.** When a job or a sale needs more than
  the pool holds, the shortfall is drawn from opening stock at the type's opening price - its median
  daily average in The Forge over the 30 days up to the cutover (`ledger_sync`). A type with no price
  at all is never costed as free: its units are counted and reported as unpriced.
* **Broker fees follow their order.** The journal does not link a fee to its order, and ESI moves an
  order's `issued` to its last modification, so a fee is matched to the order issued in the same
  second, or else to the first order its issuer modified afterwards whose value the fee fits at that
  issuer's own measured rate. A buy order's fee is part of what the stock cost; a sell order's is a
  selling cost; a fee that fits nothing is overhead.
* **Jobs consume what the recipe says.** ESI never lists a job's inputs, so they come from the SDE:
  manufacturing and reactions from the blueprint's materials at its ME, invention from its data cores
  plus one decryptor and one copy run per attempt. Fees are the journal's, linked by job id, with the
  job row's own `cost` as the fallback when the journal no longer has them.
* **Invention cost lands on the successes.** An invention job's whole cost - failed attempts
  included - becomes the value of the copy runs it produced, so a T2 unit carries the real price of
  the attempts behind it. A job that produced nothing still adds its cost to that blueprint's pool,
  where the next success absorbs it.
* **The decryptor is read off the copies.** ESI's invention rows do not name one. An invented copy
  has ME 2 and TE 4 plus the decryptor's modifiers, so the copies a sync sees identify it; every
  current decryptor has a distinct pair except Parity and Optimized Attainment, which are told apart
  by which of the two the business has actually bought.
* **Trades between our own wallets are not trades.** A transaction whose counterparty is one of the
  business's characters or corporations moves stock between pockets and is skipped on both sides.

Scopes: a product is on an *invention line* when it was manufactured from a blueprint the business
invented. Its revenue and cost of goods are reported apart from everything else (recovered stock,
surplus minerals, T1), and overhead (office rent, contract fees, research jobs, fees nothing could
be matched to) is reported apart from both.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date as _date

from . import industry

# ESI industry activity ids.
MANUFACTURING = 1
RESEARCH_TE = 3
RESEARCH_ME = 4
COPYING = 5
REVERSE_ENGINEERING = 7
INVENTION = 8
REACTIONS = (9, 11)

# What an invented copy starts at before its decryptor. Game rule, measured 2026-09-24 against the
# ledger's own copies: Augmentation (ME -2, TE +2) yields ME 0 TE 6, Symmetry (ME +1, TE +8) ME 3 TE 12.
INVENTED_ME = 2
INVENTED_TE = 4

# NPC stations have ids in this range; a job anywhere else ran in a player structure, whose rig and
# hull bonuses ESI does not report - its materials are costed without them, and the report says so.
NPC_STATIONS = range(60_000_000, 64_000_000)

# Corporation journal entries that are overhead: real costs of running the business that no unit of
# stock carries. Keyed by ESI ref_type, valued by the line they are reported under.
OVERHEAD_REF_TYPES = {
    "office_rental_fee": "office rent",
    "contract_brokers_fee_corp": "contract fees",
    "contract_brokers_fee": "contract fees",
    "contract_sales_tax": "contract fees",
    "contract_reward": "courier contracts",
}

SCOPE_INVENTION = "invention"
SCOPE_OTHER = "other"
SCOPE_OVERHEAD = "overhead"

# How far a fee may stray from its issuer's measured rate and still be matched to a modified order.
# Modifying an order moves its price by a few percent at most; a fee 30 % off the rate is some other
# order's, or a relist fee, and is better reported as unmatched than attributed by guesswork.
FEE_RATE_TOLERANCE = 0.30

JOB_DONE = {"delivered", "ready"}
JOB_LOST = {"cancelled", "reverted"}


@dataclass
class Pool:
    """Quantity and total ISK of one type of stock the ledger has costed."""
    qty: float = 0.0
    cost: float = 0.0

    @property
    def unit(self) -> float | None:
        return self.cost / self.qty if self.qty > 0 else None

    def add(self, qty: float, cost: float) -> None:
        self.qty += qty
        self.cost += cost

    def take(self, qty: float) -> tuple[float, float]:
        """(ISK taken at average, quantity the pool could not supply)."""
        if qty <= 0:
            return 0.0, 0.0
        if self.qty <= 0:
            return 0.0, qty
        have = min(self.qty, qty)
        cost = self.cost * have / self.qty
        self.qty -= have
        self.cost -= cost
        if self.qty <= 1e-9:
            self.qty = 0.0
        return cost, qty - have


@dataclass
class Entry:
    """One line of profit and loss. `amount` is signed: revenue positive, every cost negative."""
    date: str
    kind: str            # revenue | sales_tax | broker_fee | cogs | overhead | job_loss
    scope: str           # invention | other | overhead
    amount: float
    type_id: int | None = None
    qty: float = 0.0
    wallet: str | None = None
    label: str | None = None
    opening: float = 0.0  # the part of a cogs amount valued at opening-stock prices


@dataclass
class JobCost:
    """What one job consumed and made, as the replay costed it."""
    job_id: int
    activity_id: int
    status: str | None
    blueprint_type_id: int | None
    product_type_id: int | None
    runs: int
    start_date: str
    end_date: str | None
    installer_id: int | None
    materials: float = 0.0        # items consumed, pool and opening stock together
    opening: float = 0.0          # of which valued at opening-stock prices
    blueprint: float = 0.0        # copy runs consumed (their copy or invention cost)
    fees: float = 0.0
    output_qty: float = 0.0
    done: bool = False
    successes: int | None = None
    decryptor_id: int | None = None
    me: int | None = None

    @property
    def total(self) -> float:
        return self.materials + self.blueprint + self.fees


@dataclass
class Facts:
    """Everything stored, as plain rows; `load` reads it and the engine never touches SQL."""
    jobs: list[dict]
    transactions: list[dict]
    journal: list[dict]
    orders: list[dict]
    blueprints: list[dict]
    opening_prices: dict[int, float]
    internal_ids: set[int]


@dataclass
class Book:
    """The replay's result: the P&L lines, the stock left, the jobs and what was assumed."""
    entries: list[Entry] = field(default_factory=list)
    pools: dict[tuple[str, int], Pool] = field(default_factory=dict)
    jobs: dict[int, JobCost] = field(default_factory=dict)
    invention_products: set[int] = field(default_factory=set)
    decryptors: dict[int, int | None] = field(default_factory=dict)   # T2 blueprint -> decryptor
    opening_draws: dict[int, dict] = field(default_factory=dict)      # type -> qty, value, unpriced
    notes: Counter = field(default_factory=Counter)
    first_date: str | None = None
    last_date: str | None = None

    def pool(self, kind: str, type_id: int) -> Pool:
        return self.pools.setdefault((kind, type_id), Pool())


def load(conn: sqlite3.Connection) -> Facts:
    rows = lambda sql: [dict(r) for r in conn.execute(sql)]  # noqa: E731
    opening = {int(r[0]): float(r[1]) for r in conn.execute("SELECT type_id, price FROM opening_prices")}
    owners = {r[0] for r in conn.execute("SELECT DISTINCT owner FROM jobs UNION SELECT DISTINCT owner FROM orders "
                                         "UNION SELECT DISTINCT owner FROM blueprints")}
    owners |= {r[0] for r in conn.execute("SELECT DISTINCT wallet FROM transactions "
                                          "UNION SELECT DISTINCT wallet FROM journal")}
    internal = set()
    for owner in owners:
        parts = owner.split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            internal.add(int(parts[1]))
    return Facts(
        jobs=rows("SELECT * FROM jobs ORDER BY start_date, job_id"),
        transactions=rows("SELECT * FROM transactions ORDER BY date, transaction_id"),
        journal=rows("SELECT * FROM journal ORDER BY date, id"),
        orders=rows("SELECT * FROM orders"),
        blueprints=rows("SELECT * FROM blueprints"),
        opening_prices=opening,
        internal_ids=internal,
    )


def _scope_owner_wallet(wallet: str) -> tuple[str, int]:
    """'corp:98:1' -> ('corp', 98); 'char:9' -> ('char', 9)."""
    kind, ident = wallet.split(":")[:2]
    return kind, int(ident)


class _Replay:
    def __init__(self, facts: Facts, recipes: Mapping[int, industry.Recipe], invention: Mapping,
                 now: str):
        self.facts = facts
        self.recipes = recipes
        self.inv_rows = invention.get("blueprints", {})
        self.dec_rows = {int(k): v for k, v in invention.get("decryptors", {}).items()}
        # Every blueprint type invention can produce, whoever invented the copy in hand.
        self.inventable = {int(p[0]) for row in self.inv_rows.values() for p in row["p"]}
        self.now = now
        self.book = Book()
        self.bought_types = {t["type_id"] for t in facts.transactions if t["is_buy"]}
        self.blueprint_items = {b["item_id"]: b for b in facts.blueprints}
        self.fee_rates: dict[int, float] = {}
        self.decryptor_basis = "copies"   # how the last `decryptor_for` answer was reached
        self.orders_by_issuer: dict[int, list[dict]] = defaultdict(list)

    # -- helpers -------------------------------------------------------------

    def note(self, key: str, count: int = 1) -> None:
        self.book.notes[key] += count

    def _draw(self, type_id: int, qty: float) -> tuple[float, float]:
        """Take `qty` of an item: (total ISK, of which opening-stock ISK)."""
        cost, short = self.book.pool("item", type_id).take(qty)
        opening = 0.0
        if short > 0:
            draw = self.book.opening_draws.setdefault(type_id, {"qty": 0.0, "value": 0.0, "unpriced": 0.0})
            price = self.facts.opening_prices.get(type_id)
            draw["qty"] += short
            if price is None:
                draw["unpriced"] += short
                self.note("unpriced_opening_units", int(round(short)))
            else:
                opening = short * price
                draw["value"] += opening
        return cost + opening, opening

    def _is_bpo(self, blueprint_id: int | None) -> bool:
        row = self.blueprint_items.get(blueprint_id)
        return row is not None and row.get("quantity") == -1

    def _decryptor_candidates(self, me: int, te: int) -> list[int | None]:
        found: list[int | None] = [None] if (me, te) == (INVENTED_ME, INVENTED_TE) else []
        found += [d for d, row in sorted(self.dec_rows.items())
                  if (INVENTED_ME + row["me"], INVENTED_TE + row["te"]) == (me, te)]
        return found

    def _runs_per_copy(self, t2_blueprint: int, after: str | None) -> int | None:
        """The most runs manufacturing ever drew from one copy of `t2_blueprint` installed after
        `after`: a fully used invented copy's run count, when no sync saw the copy itself."""
        used: Counter = Counter()
        for j in self.facts.jobs:
            if (j["activity_id"] == MANUFACTURING and j["blueprint_type_id"] == t2_blueprint
                    and j["status"] not in JOB_LOST and (after is None or j["start_date"] >= after)):
                used[j["blueprint_id"]] += j["runs"]
        return max(used.values()) if used else None

    def decryptor_for(self, t2_blueprint: int, after: str | None) -> tuple[bool, int | None]:
        """(known, decryptor id or None for "no decryptor") for an invention of `t2_blueprint`.

        Copies first seen after the job ended are the ones it can have made, so the nearest of them
        decides; failing that, the most common ME/TE among all copies of that blueprint. When no copy
        was ever seen - all used up between two syncs - the runs manufacturing drew from a single copy
        tell the decryptor's extra runs instead."""
        copies = [b for b in self.facts.blueprints if b["type_id"] == t2_blueprint and b.get("quantity") == -2
                  and b.get("me") is not None and b.get("te") is not None]
        self.decryptor_basis = "copies"
        if copies:
            later = sorted((b for b in copies if after and b["first_seen"] >= after), key=lambda b: b["first_seen"])
            if later:
                me, te = later[0]["me"], later[0]["te"]
            else:
                (me, te), _n = Counter((b["me"], b["te"]) for b in copies).most_common(1)[0]
            candidates = self._decryptor_candidates(me, te)
        else:
            runs = self._runs_per_copy(t2_blueprint, after)
            base = next((p[1] for row in self.inv_rows.values() for p in row["p"] if int(p[0]) == t2_blueprint),
                        None)
            if runs is None or base is None:
                return False, None
            candidates = ([None] if runs == base else []) + [
                d for d, row in sorted(self.dec_rows.items()) if base + row["runs"] == runs]
            self.decryptor_basis = "runs"
        if not candidates:
            return False, None
        if len(candidates) > 1:
            bought = [c for c in candidates if c is not None and c in self.bought_types]
            if len(bought) == 1:
                return True, bought[0]
            self.note("ambiguous_decryptor")
        return True, candidates[0]

    def me_for(self, job: dict, invented: set[int]) -> int:
        """The blueprint's own ME when a sync saw it; else what its decryptor gives an invented copy;
        else the most common ME among the other copies of that blueprint type the ledger has seen
        (recovered stock was usually invented in one batch); else, for a blueprint only invention
        makes, the ME of a copy invented without a decryptor; else 0. The last two are noted: the
        first is the common case for bought or recovered copies, the second can only overstate a
        job's materials, never understate them."""
        row = self.blueprint_items.get(job["blueprint_id"])
        if row is not None and row.get("me") is not None:
            return int(row["me"])
        if job["blueprint_type_id"] in invented:
            known, dec = self.decryptor_for(job["blueprint_type_id"], None)
            if known:
                return INVENTED_ME + (self.dec_rows[dec]["me"] if dec is not None else 0)
        siblings = Counter(b["me"] for b in self.facts.blueprints
                           if b["type_id"] == job["blueprint_type_id"] and b.get("me") is not None)
        if siblings:
            self.note("me_from_sibling_copies")
            return int(siblings.most_common(1)[0][0])
        if job["blueprint_type_id"] in self.inventable:
            self.note("me_assumed_invented")
            return INVENTED_ME
        self.note("unknown_me")
        return 0

    # -- events ----------------------------------------------------------------

    def run(self) -> Book:
        facts, book = self.facts, self.book
        jobs = facts.jobs
        invented = {j["product_type_id"] for j in jobs
                    if j["activity_id"] == INVENTION and j["product_type_id"] is not None}
        book.invention_products = {j["product_type_id"] for j in jobs
                                   if j["activity_id"] == MANUFACTURING and j["blueprint_type_id"] in invented
                                   and j["product_type_id"] is not None}

        fees_by_job: dict[int, float] = defaultdict(float)
        job_ids = {j["job_id"] for j in jobs}
        events: list[tuple[str, int, int, object]] = []
        seq = 0

        def push(date: str, order: int, payload) -> None:
            nonlocal seq
            seq += 1
            events.append((date, order, seq, payload))

        # Journal: industry fees onto their job, overhead onto the P&L, tax and broker fees matched.
        sells_by_stamp: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for t in facts.transactions:
            if not t["is_buy"] and t["client_id"] not in facts.internal_ids:
                sells_by_stamp[(t["wallet"], t["date"])].append(t)
        orders_by_stamp: dict[str, list[dict]] = defaultdict(list)
        for o in facts.orders:
            orders_by_stamp[o["issued"]].append(o)
            if o["issued_by"] is not None:
                self.orders_by_issuer[o["issued_by"]].append(o)
        for issuer in self.orders_by_issuer.values():
            issuer.sort(key=lambda o: o["issued"])
        # Each issuer's broker rate, measured on fees that matched exactly one order by the second.
        ratios: dict[int, list[float]] = defaultdict(list)
        for e in facts.journal:
            if e["ref_type"] != "brokers_fee" or not e["amount"]:
                continue
            matched = orders_by_stamp.get(e["date"], [])
            if len(matched) == 1 and (matched[0]["price"] or 0) * (matched[0]["volume_total"] or 0) > 0:
                o = matched[0]
                ratios[e["first_party_id"]].append(-e["amount"] / (o["price"] * o["volume_total"]))
        self.fee_rates = {issuer: sorted(r)[len(r) // 2] for issuer, r in ratios.items() if r}

        for e in facts.journal:
            amount = e["amount"] or 0.0
            kind, _owner = _scope_owner_wallet(e["wallet"])
            if e["context_id_type"] == "industry_job_id":
                if e["context_id"] in job_ids:
                    fees_by_job[e["context_id"]] += -amount
                else:
                    book.entries.append(Entry(e["date"], "overhead", SCOPE_OVERHEAD, amount,
                                              wallet=e["wallet"], label="industry fees, job not in ledger"))
            elif e["ref_type"] == "transaction_tax":
                push(e["date"], 5, ("sales_tax", e))
            elif e["ref_type"] == "brokers_fee":
                push(e["date"], 1, ("broker_fee", e))
            elif kind == "corp" and e["ref_type"] in OVERHEAD_REF_TYPES:
                book.entries.append(Entry(e["date"], "overhead", SCOPE_OVERHEAD, amount, wallet=e["wallet"],
                                          label=OVERHEAD_REF_TYPES[e["ref_type"]]))

        for t in facts.transactions:
            if t["client_id"] in facts.internal_ids:
                self.note("internal_transfers")
                continue
            push(t["date"], 0 if t["is_buy"] else 4, ("trade", t))

        for j in jobs:
            cost = JobCost(job_id=j["job_id"], activity_id=j["activity_id"], status=j["status"],
                           blueprint_type_id=j["blueprint_type_id"], product_type_id=j["product_type_id"],
                           runs=j["runs"], start_date=j["start_date"], end_date=j["end_date"],
                           installer_id=j["installer_id"])
            if j["job_id"] in fees_by_job:
                cost.fees = fees_by_job[j["job_id"]]
            elif j["cost"] is not None:
                cost.fees = float(j["cost"])
                self.note("fees_from_job_row")
            book.jobs[j["job_id"]] = cost
            push(j["start_date"], 2, ("start", j))
            status = j["status"]
            end = j["completed_date"] or j["end_date"]
            if status in JOB_LOST:
                push(end or j["start_date"], 3, ("lost", j))
            elif status in JOB_DONE or (status in ("active", "paused") and j["end_date"] and j["end_date"] <= self.now
                                        and j["activity_id"] != INVENTION):
                push(end or j["start_date"], 3, ("finish", j))

        events.sort(key=lambda ev: (ev[0], ev[1], ev[2]))
        for date, _order, _seq, (kind, payload) in events:
            book.first_date = book.first_date or date
            book.last_date = date
            getattr(self, f"_on_{kind}")(date, payload, sells_by_stamp, orders_by_stamp, invented)
        return book

    def _scope(self, type_id: int | None) -> str:
        return SCOPE_INVENTION if type_id in self.book.invention_products else SCOPE_OTHER

    def _on_trade(self, date, t, *_):
        pool = self.book.pool("item", t["type_id"])
        value = t["quantity"] * t["unit_price"]
        if t["is_buy"]:
            pool.add(t["quantity"], value)
            return
        scope = self._scope(t["type_id"])
        cost, opening = self._draw(t["type_id"], t["quantity"])
        self.book.entries.append(Entry(date, "revenue", scope, value, t["type_id"], t["quantity"], t["wallet"]))
        self.book.entries.append(Entry(date, "cogs", scope, -cost, t["type_id"], t["quantity"], t["wallet"],
                                       opening=-opening))

    def _on_sales_tax(self, date, e, sells_by_stamp, *_):
        amount = e["amount"] or 0.0
        sells = sells_by_stamp.get((e["wallet"], date), [])
        total = sum(s["quantity"] * s["unit_price"] for s in sells)
        if not sells or total <= 0:
            self.book.entries.append(Entry(date, "sales_tax", SCOPE_OVERHEAD, amount, wallet=e["wallet"],
                                           label="sales tax, sale not matched"))
            return
        for s in sells:
            share = amount * s["quantity"] * s["unit_price"] / total
            self.book.entries.append(Entry(date, "sales_tax", self._scope(s["type_id"]), share, s["type_id"],
                                           wallet=e["wallet"]))

    def _on_broker_fee(self, date, e, _sells, orders_by_stamp, *_):
        amount = e["amount"] or 0.0
        candidates = [o for o in orders_by_stamp.get(date, [])
                      if o["issued_by"] in (None, e["first_party_id"])] or orders_by_stamp.get(date, [])
        if not candidates:
            later = self._modified_order(date, e["first_party_id"], -amount)
            candidates = [later] if later else []
            if later:
                self.note("broker_fees_matched_by_rate")
        weight = sum((o["price"] or 0) * (o["volume_total"] or 0) for o in candidates)
        if not candidates or weight <= 0:
            self.book.entries.append(Entry(date, "broker_fee", SCOPE_OVERHEAD, amount, wallet=e["wallet"],
                                           label="broker fee, order not matched"))
            return
        for o in candidates:
            share = amount * (o["price"] or 0) * (o["volume_total"] or 0) / weight
            if o["is_buy"]:
                self.book.pool("item", o["type_id"]).add(0.0, -share)   # part of what the stock cost
            else:
                self.book.entries.append(Entry(date, "broker_fee", self._scope(o["type_id"]), share,
                                               o["type_id"], wallet=e["wallet"]))

    def _modified_order(self, date: str, issuer: int | None, fee: float) -> dict | None:
        """The first order `issuer` last touched after `date` whose value `fee` fits at their rate."""
        rate = self.fee_rates.get(issuer)
        if not rate or fee <= 0:
            return None
        for o in self.orders_by_issuer.get(issuer, []):
            if o["issued"] <= date:
                continue
            value = (o["price"] or 0) * (o["volume_total"] or 0)
            if value > 0 and abs(fee / value - rate) <= rate * FEE_RATE_TOLERANCE:
                return o
        return None

    def _on_start(self, date, j, _s, _o, invented):
        cost = self.book.jobs[j["job_id"]]
        activity = j["activity_id"]
        runs = j["runs"]
        if j["facility_id"] is not None and j["facility_id"] not in NPC_STATIONS and \
                activity in (MANUFACTURING, *REACTIONS):
            self.note("structure_jobs")
        if activity in (MANUFACTURING, *REACTIONS):
            recipe = self.recipes.get(j["blueprint_type_id"])
            if recipe is None:
                self.note("missing_recipe")
                return
            me = self.me_for(j, invented) if recipe.researchable else 0
            me = max(0, min(me, industry.MAX_ME))
            cost.me = me
            for material, base in recipe.materials.items():
                total, opening = self._draw(material, industry.required_quantity(base, runs, me))
                cost.materials += total
                cost.opening += opening
            if not self._is_bpo(j["blueprint_id"]):
                taken, short = self.book.pool("bpc", j["blueprint_type_id"]).take(runs)
                cost.blueprint += taken
                if short:
                    self.note("pre_ledger_copy_runs", int(short))
        elif activity == INVENTION:
            row = self.inv_rows.get(str(j["blueprint_type_id"]))
            if row is None:
                self.note("missing_invention_recipe")
                return
            for material, per_attempt in row["m"].items():
                total, opening = self._draw(int(material), per_attempt * runs)
                cost.materials += total
                cost.opening += opening
            known, dec = self.decryptor_for(j["product_type_id"], j["end_date"])
            if not known:
                self.note("unknown_decryptor")
            elif self.decryptor_basis == "runs":
                self.note("decryptor_from_runs")
            cost.decryptor_id = dec
            if known:
                self.book.decryptors[j["product_type_id"]] = dec
            if dec is not None:
                total, opening = self._draw(dec, runs)
                cost.materials += total
                cost.opening += opening
            taken, short = self.book.pool("bpc", j["blueprint_type_id"]).take(runs)
            cost.blueprint += taken
            if short:
                self.note("pre_ledger_copy_runs", int(short))

    def _on_finish(self, date, j, *_):
        cost = self.book.jobs[j["job_id"]]
        activity = j["activity_id"]
        cost.done = True
        if activity in (MANUFACTURING, *REACTIONS):
            recipe = self.recipes.get(j["blueprint_type_id"])
            qty = j["runs"] * (recipe.product_qty if recipe else 1)
            cost.output_qty = qty
            if j["product_type_id"] is not None:
                self.book.pool("item", j["product_type_id"]).add(qty, cost.total)
        elif activity == COPYING:
            qty = j["runs"] * (j["licensed_runs"] or 1)
            cost.output_qty = qty
            self.book.pool("bpc", j["blueprint_type_id"]).add(qty, cost.total)
        elif activity == INVENTION:
            successes = j["successful_runs"]
            if successes is None:
                cost.done = False      # delivered without an outcome: nothing to book yet
                self.note("invention_without_outcome")
                return
            row = self.inv_rows.get(str(j["blueprint_type_id"])) or {"p": []}
            base = next((p[1] for p in row["p"] if int(p[0]) == j["product_type_id"]), 1)
            extra = self.dec_rows[cost.decryptor_id]["runs"] if cost.decryptor_id in self.dec_rows else 0
            cost.successes = successes
            cost.output_qty = successes * (base + extra)
            self.book.pool("bpc", j["product_type_id"]).add(cost.output_qty, cost.total)
        else:
            # Research and anything else: a real cost that no unit of stock carries.
            self.book.entries.append(Entry(date, "overhead", SCOPE_OVERHEAD, -cost.total,
                                           label="research jobs"))

    def _on_lost(self, date, j, *_):
        cost = self.book.jobs[j["job_id"]]
        self.book.entries.append(Entry(date, "job_loss", SCOPE_OVERHEAD, -cost.total,
                                       j["product_type_id"], label=f"{j['status']} jobs"))


def replay(facts: Facts, recipes: Mapping[int, industry.Recipe], invention: Mapping, now: str) -> Book:
    """Replay every stored fact in time order. `now` is ESI's clock, as an ISO stamp."""
    return _Replay(facts, recipes, invention, now).run()


# ---------------------------------------------------------------------------
# reports over a Book
# ---------------------------------------------------------------------------

def _in(date: str, since: str | None, until: str | None) -> bool:
    return (since is None or date >= since) and (until is None or date < until)


def period_key(date: str, by: str) -> str:
    if by == "day":
        return date[:10]
    if by == "month":
        return date[:7]
    if by == "week":
        year, week, _ = _date.fromisoformat(date[:10]).isocalendar()
        return f"{year}-W{week:02d}"
    return "total"


_PNL_LINES = ("revenue", "sales_tax", "broker_fees", "cogs", "cogs_opening_stock")
_ENTRY_LINE = {"revenue": "revenue", "sales_tax": "sales_tax", "broker_fee": "broker_fees", "cogs": "cogs"}


def pnl(book: Book, since: str | None = None, until: str | None = None, by: str = "total") -> list[dict]:
    """Profit and loss per period: each line split into invention lines, other trade and their total,
    then overhead (which belongs to the business, not to either scope) and net profit."""
    periods: dict[str, dict] = {}
    for e in book.entries:
        if not _in(e.date, since, until):
            continue
        key = period_key(e.date, by)
        row = periods.setdefault(key, {
            "period": key, "from": e.date, "to": e.date,
            **{scope: dict.fromkeys(_PNL_LINES, 0.0) for scope in (SCOPE_INVENTION, SCOPE_OTHER)},
            "overhead": defaultdict(float), "revenue_personal_wallets": 0.0})
        row["from"], row["to"] = min(row["from"], e.date), max(row["to"], e.date)
        if e.scope == SCOPE_OVERHEAD:
            row["overhead"][e.label or e.kind] += e.amount
            continue
        lines = row[e.scope]
        lines[_ENTRY_LINE[e.kind]] += e.amount
        if e.kind == "cogs":
            lines["cogs_opening_stock"] += e.opening
        if e.kind == "revenue" and e.wallet and e.wallet.startswith("char:"):
            row["revenue_personal_wallets"] += e.amount
    out = []
    for key in sorted(periods):
        row = periods[key]
        for scope in (SCOPE_INVENTION, SCOPE_OTHER):
            lines = row[scope]
            lines["gross_profit"] = lines["revenue"] + lines["sales_tax"] + lines["broker_fees"] + lines["cogs"]
        row["total"] = {line: row[SCOPE_INVENTION][line] + row[SCOPE_OTHER][line]
                        for line in (*_PNL_LINES, "gross_profit")}
        row["overhead"] = dict(sorted(row["overhead"].items()))
        row["overhead_total"] = sum(row["overhead"].values())
        row["net_profit"] = row["total"]["gross_profit"] + row["overhead_total"]
        out.append(row)
    return out


def products(book: Book, since: str | None = None, until: str | None = None, scope: str = "all") -> list[dict]:
    """Per product: what was built and at what unit cost, what was sold and what it made."""
    rows: dict[int, dict] = {}

    def row(type_id: int) -> dict:
        return rows.setdefault(type_id, {
            "type_id": type_id, "scope": SCOPE_INVENTION if type_id in book.invention_products else SCOPE_OTHER,
            "built": 0.0, "build_cost": 0.0, "materials": 0.0, "blueprint": 0.0, "fees": 0.0,
            "opening_stock": 0.0, "sold": 0.0, "revenue": 0.0, "sales_tax": 0.0, "broker_fees": 0.0,
            "cogs": 0.0})

    for job in book.jobs.values():
        if job.activity_id not in (MANUFACTURING, *REACTIONS) or not job.done or job.product_type_id is None:
            continue
        if not _in(job.end_date or job.start_date, since, until):
            continue
        r = row(job.product_type_id)
        r["built"] += job.output_qty
        r["build_cost"] += job.total
        r["materials"] += job.materials
        r["blueprint"] += job.blueprint
        r["fees"] += job.fees
        r["opening_stock"] += job.opening
    for e in book.entries:
        if e.type_id is None or e.scope == SCOPE_OVERHEAD or not _in(e.date, since, until):
            continue
        r = row(e.type_id)
        if e.kind == "revenue":
            r["sold"] += e.qty
            r["revenue"] += e.amount
        elif e.kind == "sales_tax":
            r["sales_tax"] += e.amount
        elif e.kind == "broker_fee":
            r["broker_fees"] += e.amount
        elif e.kind == "cogs":
            r["cogs"] += e.amount
    out = []
    for r in rows.values():
        if scope != "all" and r["scope"] != scope:
            continue
        if not r["built"] and not r["sold"]:
            continue
        r["unit_cost"] = r["build_cost"] / r["built"] if r["built"] else None
        r["profit"] = r["revenue"] + r["sales_tax"] + r["broker_fees"] + r["cogs"]
        r["net_per_unit"] = ((r["revenue"] + r["sales_tax"] + r["broker_fees"]) / r["sold"]
                             if r["sold"] else None)
        r["margin_pct"] = r["profit"] / r["revenue"] * 100 if r["revenue"] else None
        out.append(r)
    out.sort(key=lambda r: (-r["profit"], -r["build_cost"]))
    return out


def invention(book: Book, invention_doc: Mapping, recipes: Mapping[int, industry.Recipe]) -> list[dict]:
    """Per invented blueprint: attempts, outcomes against the modelled chance, and cost per run."""
    decryptor_rows = {int(k): v for k, v in invention_doc.get("decryptors", {}).items()}
    rows: dict[int, dict] = {}
    for job in book.jobs.values():
        if job.activity_id != INVENTION or job.product_type_id is None:
            continue
        r = rows.setdefault(job.product_type_id, {
            "blueprint_type_id": job.product_type_id, "jobs": 0, "attempts": 0, "attempts_done": 0,
            "successes": 0, "runs_made": 0.0, "cost_done": 0.0, "cost_running": 0.0,
            "materials": 0.0, "copy": 0.0, "fees": 0.0, "decryptor_id": None, "decryptor_known": False})
        r["jobs"] += 1
        r["attempts"] += job.runs
        r["materials"] += job.materials
        r["copy"] += job.blueprint
        r["fees"] += job.fees
        if job.done:
            r["attempts_done"] += job.runs
            r["successes"] += job.successes or 0
            r["runs_made"] += job.output_qty
            r["cost_done"] += job.total
        else:
            r["cost_running"] += job.total
    for product, r in rows.items():
        dec = book.decryptors.get(product)
        r["decryptor_known"] = product in book.decryptors
        r["decryptor_id"] = dec
        r["decryptor"] = (decryptor_rows.get(dec, {}).get("name") if dec is not None
                          else ("none" if r["decryptor_known"] else None))
        r["success_rate"] = r["successes"] / r["attempts_done"] if r["attempts_done"] else None
        r["cost_per_attempt"] = ((r["materials"] + r["copy"] + r["fees"]) / r["attempts"]
                                 if r["attempts"] else None)
        r["cost_per_run"] = r["cost_done"] / r["runs_made"] if r["runs_made"] else None
        recipe = recipes.get(product)
        r["product_type_id"] = recipe.product_id if recipe else None
        r["cost_per_unit"] = (r["cost_per_run"] / recipe.product_qty
                              if recipe and r["cost_per_run"] is not None else None)
    return sorted(rows.values(), key=lambda r: -(r["cost_done"] + r["cost_running"]))


def work_in_progress(book: Book) -> list[dict]:
    """Jobs installed and not yet finished, with the ISK already sunk into them."""
    out = []
    for job in book.jobs.values():
        if job.done or job.status in JOB_LOST or job.activity_id in (RESEARCH_ME, RESEARCH_TE):
            continue
        out.append({"job_id": job.job_id, "activity_id": job.activity_id,
                    "blueprint_type_id": job.blueprint_type_id, "product_type_id": job.product_type_id,
                    "runs": job.runs, "end_date": job.end_date, "cost": job.total,
                    "installer_id": job.installer_id})
    return sorted(out, key=lambda r: r["end_date"] or "")


_ACTIVITY_NAMES = {MANUFACTURING: "manufacturing", INVENTION: "invention", COPYING: "copying",
                   RESEARCH_ME: "research", RESEARCH_TE: "research", **{r: "reactions" for r in REACTIONS}}


def snapshot(book: Book, facts: Facts, now: str, since: str | None = None) -> dict:
    """The operation's figures at `now`, as one flat-ish document a sync stores and `progress` lists.

    Everything here is derived from what is stored, so an older snapshot can always be told apart
    from a newer costing rule by its date alone - which is why each sync keeps its own row rather than
    anything being recomputed backwards."""
    running: Counter = Counter()
    ready: Counter = Counter()
    for j in facts.jobs:
        name = _ACTIVITY_NAMES.get(j["activity_id"], "other")
        if j["status"] == "ready" or (j["status"] == "active" and j["end_date"] and j["end_date"] <= now):
            ready[name] += 1
        elif j["status"] in ("active", "paused"):
            running[name] += 1
    open_orders = [o for o in facts.orders if o["state"] == "open"]
    buys = [o for o in open_orders if o["is_buy"]]
    sells = [o for o in open_orders if not o["is_buy"]]
    (row,) = pnl(book, since=since) or [None]
    total = row["total"] if row else {}
    invention_lines = row[SCOPE_INVENTION] if row else {}
    built = sold = 0.0
    for p in products(book, since=since, scope=SCOPE_INVENTION):
        built += p["built"]
        sold += p["sold"]
    return {
        "jobs_running": dict(running), "jobs_ready": dict(ready),
        "buy_orders": len(buys),
        "buy_orders_remaining_isk": round(sum((o["price"] or 0) * (o["volume_remain"] or 0) for o in buys), 2),
        "sell_orders": len(sells),
        "sell_orders_listed_isk": round(sum((o["price"] or 0) * (o["volume_remain"] or 0) for o in sells), 2),
        "pnl_since": since,
        "invention_revenue": round(invention_lines.get("revenue", 0.0), 2),
        "invention_gross_profit": round(invention_lines.get("gross_profit", 0.0), 2),
        "total_revenue": round(total.get("revenue", 0.0), 2),
        "total_gross_profit": round(total.get("gross_profit", 0.0), 2),
        "overhead": round(row["overhead_total"], 2) if row else 0.0,
        "net_profit": round(row["net_profit"], 2) if row else 0.0,
        "invention_units_built": built, "invention_units_sold": sold,
        "stock_at_cost": round(sum(p.cost for (kind, _t), p in book.pools.items() if kind == "item" and p.qty > 0), 2),
        "work_in_progress_cost": round(sum(j["cost"] for j in work_in_progress(book)), 2),
    }
