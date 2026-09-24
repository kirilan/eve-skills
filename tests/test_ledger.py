"""The accounting ledger: the store, the replay's costing rules, the sync and the commands.

Engine tests hand `ledger.replay` synthetic facts and a synthetic recipe set - ids invented below -
so a failure points at a costing rule rather than at this week's SDE or at ESI. The sync and command
tests run the real CLI against the fake ESI in `fake_esi`, in live ESI's own row shapes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from eve_skills import doctor, industry, ledger, ledger_db, ledger_sync

from .fake_esi import ADA, CORP_SHARED, FakeEsiEnv


# -- a synthetic industry --------------------------------------------------------------------------

T1_BP = 700900          # T1 blueprint, copied and invented from
T2_BP = 701000          # invented T2 blueprint
T2_ITEM = 702000        # what the T2 blueprint builds
PART = 703010           # manufacturing input, 5 per run
CORE = 703020           # data core, 2 per attempt
MINERAL = 703030        # something only ever sold from old stock
AUG, SYM, PARITY, OPT_ATTAIN = 34203, 34206, 34204, 34207
CORP = 98000111
CHAR = 91000123
STRANGER = 95000000
WALLET = f"corp:{CORP}:1"

RECIPES = {T2_BP: industry.Recipe(blueprint_id=T2_BP, activity="manufacturing", product_id=T2_ITEM,
                                  product_qty=1, time=600, max_runs=10, materials={PART: 5},
                                  alternatives=())}
INVENTION = {
    "blueprints": {str(T1_BP): {"m": {str(CORE): 2}, "p": [[str(T2_BP), 1, 0.34]], "t": 3600}},
    "decryptors": {
        str(AUG): {"name": "Augmentation Decryptor", "probability": 0.6, "me": -2, "te": 2, "runs": 9},
        str(SYM): {"name": "Symmetry Decryptor", "probability": 1.0, "me": 1, "te": 8, "runs": 2},
        str(PARITY): {"name": "Parity Decryptor", "probability": 1.5, "me": 1, "te": -2, "runs": 3},
        str(OPT_ATTAIN): {"name": "Optimized Attainment Decryptor", "probability": 1.9, "me": 1, "te": -2,
                          "runs": 2},
    },
}


def stamp(day: int, hour: int = 12, minute: int = 0) -> str:
    return f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00Z"


_ids = iter(range(1, 10_000))


def tx(day, type_id, qty, price, *, buy, client=STRANGER, wallet=WALLET, hour=12, minute=0):
    return {"wallet": wallet, "transaction_id": next(_ids), "date": stamp(day, hour, minute), "type_id": type_id,
            "quantity": qty, "unit_price": price, "is_buy": 1 if buy else 0, "client_id": client,
            "location_id": 60003760, "journal_ref_id": None}


def journal(day, ref_type, amount, *, context=None, context_type=None, party=CHAR, wallet=WALLET,
            hour=12, minute=0):
    return {"wallet": wallet, "id": next(_ids), "date": stamp(day, hour, minute), "ref_type": ref_type,
            "amount": amount, "context_id": context, "context_id_type": context_type,
            "first_party_id": party, "second_party_id": None}


def job(job_id, activity, *, blueprint_type, product, runs, start, end, status="delivered", cost=0.0,
        blueprint_id=None, licensed_runs=None, successes=None, facility=60002329):
    return {"job_id": job_id, "owner": f"corp:{CORP}", "activity_id": activity, "status": status,
            "installer_id": CHAR, "facility_id": facility, "blueprint_id": blueprint_id or job_id * 10,
            "blueprint_type_id": blueprint_type, "product_type_id": product, "runs": runs,
            "licensed_runs": licensed_runs, "successful_runs": successes, "probability": 0.4, "cost": cost,
            "start_date": start, "end_date": end, "completed_date": None}


def order(order_id, type_id, *, buy, issued, price, volume, issued_by=CHAR, state="open"):
    return {"order_id": order_id, "owner": f"corp:{CORP}", "type_id": type_id, "is_buy": 1 if buy else 0,
            "issued": issued, "issued_by": issued_by, "price": price, "volume_total": volume,
            "volume_remain": 0, "state": state, "location_id": 60003760}


def copy(item_id, type_id, me, te, *, seen=stamp(20), quantity=-2):
    return {"item_id": item_id, "owner": f"corp:{CORP}", "type_id": type_id, "quantity": quantity, "me": me,
            "te": te, "runs": 10, "first_seen": seen, "last_seen": seen}


def facts(*, jobs=(), transactions=(), entries=(), orders=(), blueprints=(), opening=None) -> ledger.Facts:
    return ledger.Facts(jobs=list(jobs), transactions=sorted(transactions, key=lambda t: t["date"]),
                        journal=sorted(entries, key=lambda e: e["date"]), orders=list(orders),
                        blueprints=list(blueprints), opening_prices=dict(opening or {}),
                        internal_ids={CORP, CHAR})


def replay(f: ledger.Facts, now: str = stamp(28)) -> ledger.Book:
    return ledger.replay(f, RECIPES, INVENTION, now)


def lines(book: ledger.Book, kind: str) -> list[ledger.Entry]:
    return [e for e in book.entries if e.kind == kind]


# -- engine ----------------------------------------------------------------------------------------

class WeightedAverageTests(unittest.TestCase):
    def test_a_sale_costs_the_average_of_everything_bought_before_it(self):
        book = replay(facts(transactions=[tx(1, PART, 10, 100.0, buy=True), tx(2, PART, 10, 200.0, buy=True),
                                          tx(3, PART, 4, 500.0, buy=False)]))
        (cogs,) = lines(book, "cogs")
        self.assertAlmostEqual(-600.0, cogs.amount)          # 4 x the 150 average
        self.assertAlmostEqual(2000.0, lines(book, "revenue")[0].amount)
        self.assertAlmostEqual(16 * 150.0, book.pools[("item", PART)].cost)

    def test_stock_the_ledger_never_saw_bought_is_valued_at_its_opening_price(self):
        book = replay(facts(transactions=[tx(1, MINERAL, 100, 9.0, buy=False)], opening={MINERAL: 7.0}))
        (cogs,) = lines(book, "cogs")
        self.assertAlmostEqual(-700.0, cogs.amount)
        self.assertAlmostEqual(-700.0, cogs.opening)
        self.assertEqual({"qty": 100.0, "value": 700.0, "unpriced": 0.0}, book.opening_draws[MINERAL])

    def test_opening_stock_without_a_price_is_counted_never_silently_free(self):
        book = replay(facts(transactions=[tx(1, MINERAL, 30, 9.0, buy=False)]))
        self.assertEqual(30, book.notes["unpriced_opening_units"])
        self.assertEqual(30.0, book.opening_draws[MINERAL]["unpriced"])

    def test_a_trade_between_our_own_wallets_moves_no_stock_and_books_nothing(self):
        book = replay(facts(transactions=[tx(1, PART, 5, 100.0, buy=True, client=CHAR),
                                          tx(1, PART, 5, 100.0, buy=False, client=CORP, wallet=f"char:{CHAR}")]))
        self.assertEqual([], book.entries)
        self.assertNotIn(("item", PART), book.pools)
        self.assertEqual(2, book.notes["internal_transfers"])


class JobCostTests(unittest.TestCase):
    def test_manufacturing_consumes_the_recipe_at_the_copys_me_and_charges_the_journal_fee(self):
        book = replay(facts(
            transactions=[tx(1, PART, 100, 10.0, buy=True)],
            jobs=[job(1, ledger.MANUFACTURING, blueprint_type=T2_BP, product=T2_ITEM, runs=10,
                      start=stamp(2), end=stamp(3), blueprint_id=555, cost=1.0)],
            entries=[journal(2, "manufacturing", -40.0, context=1, context_type="industry_job_id"),
                     journal(2, "industry_job_tax", -2.0, context=1, context_type="industry_job_id")],
            blueprints=[copy(555, T2_BP, 4, 4)]))
        cost = book.jobs[1]
        self.assertEqual(4, cost.me)
        self.assertAlmostEqual(480.0, cost.materials)        # ceil(10 x 5 x 0.96) = 48 parts at 10
        self.assertAlmostEqual(42.0, cost.fees)              # the journal, not the job row's 1.0
        self.assertEqual(10, cost.output_qty)
        self.assertAlmostEqual(522.0, book.pools[("item", T2_ITEM)].cost)
        self.assertEqual(10, book.notes["pre_ledger_copy_runs"])   # the copy predates the ledger

    def test_the_job_rows_cost_stands_in_when_the_journal_has_forgotten_the_fee(self):
        book = replay(facts(jobs=[job(1, ledger.MANUFACTURING, blueprint_type=T2_BP, product=T2_ITEM, runs=1,
                                      start=stamp(2), end=stamp(3), cost=77.0)],
                            blueprints=[copy(10, T2_BP, 2, 4)], opening={PART: 1.0}))
        self.assertAlmostEqual(77.0, book.jobs[1].fees)
        self.assertEqual(1, book.notes["fees_from_job_row"])

    def test_a_cancelled_job_is_a_loss_not_stock(self):
        book = replay(facts(jobs=[job(1, ledger.MANUFACTURING, blueprint_type=T2_BP, product=T2_ITEM, runs=1,
                                      start=stamp(2), end=stamp(3), status="cancelled", cost=50.0)],
                            blueprints=[copy(10, T2_BP, 2, 4)], opening={PART: 1.0}))
        (loss,) = lines(book, "job_loss")
        self.assertAlmostEqual(-55.0, loss.amount)
        self.assertNotIn(("item", T2_ITEM), book.pools)

    def test_a_running_job_is_work_in_progress_until_it_ends(self):
        book = replay(facts(jobs=[job(1, ledger.MANUFACTURING, blueprint_type=T2_BP, product=T2_ITEM, runs=2,
                                      start=stamp(20), end=stamp(30), status="active", cost=5.0)],
                            blueprints=[copy(10, T2_BP, 2, 4)], opening={PART: 1.0}), now=stamp(25))
        (wip,) = ledger.work_in_progress(book)
        self.assertEqual(1, wip["job_id"])
        self.assertAlmostEqual(15.0, wip["cost"])

    def test_a_blueprint_nobody_saw_is_costed_at_the_me_invention_gives_without_a_decryptor(self):
        book = replay(facts(jobs=[job(1, ledger.MANUFACTURING, blueprint_type=T2_BP, product=T2_ITEM, runs=10,
                                      start=stamp(2), end=stamp(3))], opening={PART: 1.0}))
        self.assertEqual(ledger.INVENTED_ME, book.jobs[1].me)
        self.assertEqual(1, book.notes["me_assumed_invented"])


class InventionTests(unittest.TestCase):
    def invention(self, *, successes, runs=10, blueprints=(), start=stamp(2), end=stamp(3), job_id=1):
        return job(job_id, ledger.INVENTION, blueprint_type=T1_BP, product=T2_BP, runs=runs, start=start,
                   end=end, successes=successes)

    def test_every_attempts_cost_lands_on_the_runs_the_successes_made(self):
        book = replay(facts(
            transactions=[tx(1, CORE, 20, 100.0, buy=True), tx(1, AUG, 10, 1000.0, buy=True)],
            jobs=[job(9, ledger.COPYING, blueprint_type=T1_BP, product=T1_BP, runs=1, licensed_runs=10,
                      start=stamp(1, 13), end=stamp(1, 14), cost=50.0),
                  self.invention(successes=3)],
            blueprints=[copy(1, T2_BP, 0, 6, seen=stamp(4))]))
        cost = book.jobs[1]
        self.assertEqual(AUG, cost.decryptor_id)
        self.assertAlmostEqual(2000.0 + 10000.0, cost.materials)   # 2 cores + 1 decryptor per attempt
        self.assertAlmostEqual(50.0, cost.blueprint)               # all ten runs of the T1 copy
        self.assertEqual(30, cost.output_qty)                      # 3 successes x (1 + 9) runs
        pool = book.pools[("bpc", T2_BP)]
        self.assertAlmostEqual(12050.0 / 30, pool.unit)

    def test_a_job_that_invented_nothing_still_charges_the_next_success(self):
        book = replay(facts(jobs=[self.invention(successes=0, job_id=1),
                                  self.invention(successes=1, job_id=2, start=stamp(4), end=stamp(5))],
                            blueprints=[copy(1, T2_BP, 2, 4, seen=stamp(6))], opening={CORE: 10.0}))
        pool = book.pools[("bpc", T2_BP)]
        self.assertEqual(1, pool.qty)                               # no decryptor: 1 run per success
        self.assertAlmostEqual(2 * 10 * 2 * 10.0, pool.cost)        # both jobs' data cores

    def test_the_decryptor_is_read_off_the_first_copy_seen_after_the_job(self):
        book = replay(facts(jobs=[self.invention(successes=1)],
                            blueprints=[copy(1, T2_BP, 0, 6, seen=stamp(1)),        # older: not this job's
                                        copy(2, T2_BP, 3, 12, seen=stamp(5))], opening={CORE: 1.0, SYM: 1.0}))
        self.assertEqual(SYM, book.jobs[1].decryptor_id)

    def test_parity_and_optimized_attainment_are_told_apart_by_what_was_bought(self):
        book = replay(facts(transactions=[tx(1, OPT_ATTAIN, 10, 5.0, buy=True)],
                            jobs=[self.invention(successes=1)], blueprints=[copy(1, T2_BP, 3, 2, seen=stamp(5))],
                            opening={CORE: 1.0}))
        self.assertEqual(OPT_ATTAIN, book.jobs[1].decryptor_id)
        self.assertEqual(0, book.notes["ambiguous_decryptor"])

    def test_with_no_copy_ever_seen_the_runs_one_copy_built_name_the_decryptor(self):
        book = replay(facts(jobs=[self.invention(successes=1),
                                  job(2, ledger.MANUFACTURING, blueprint_type=T2_BP, product=T2_ITEM, runs=6,
                                      start=stamp(4), end=stamp(5), blueprint_id=888),
                                  job(3, ledger.MANUFACTURING, blueprint_type=T2_BP, product=T2_ITEM, runs=4,
                                      start=stamp(6), end=stamp(7), blueprint_id=888)],
                            opening={CORE: 1.0, AUG: 1.0, PART: 1.0}))
        self.assertEqual(AUG, book.jobs[1].decryptor_id)          # 6 + 4 = 10 runs = 1 + Augmentation's 9
        self.assertEqual(1, book.notes["decryptor_from_runs"])
        self.assertEqual(10, book.notes["pre_ledger_copy_runs"])  # the T1 copy's ten attempts, nothing more:
        self.assertEqual(0, book.pools[("bpc", T2_BP)].qty)       # both builds drew on the invented runs

    def test_an_unidentifiable_decryptor_is_noted_and_not_guessed(self):
        book = replay(facts(jobs=[self.invention(successes=1)], opening={CORE: 1.0}))
        self.assertIsNone(book.jobs[1].decryptor_id)
        self.assertEqual(1, book.notes["unknown_decryptor"])
        self.assertNotIn(T2_BP, book.decryptors)
        (row,) = ledger.invention(book, INVENTION, RECIPES)
        self.assertFalse(row["decryptor_known"])
        self.assertIsNone(row["decryptor"])


class FeeTests(unittest.TestCase):
    def test_a_buy_orders_broker_fee_is_part_of_what_the_stock_cost(self):
        book = replay(facts(transactions=[tx(2, PART, 10, 100.0, buy=True)],
                            entries=[journal(1, "brokers_fee", -20.0)],
                            orders=[order(1, PART, buy=True, issued=stamp(1), price=100.0, volume=10)]))
        self.assertAlmostEqual(102.0, book.pools[("item", PART)].unit)
        self.assertEqual([], lines(book, "broker_fee"))

    def test_a_sell_orders_fee_and_the_sales_tax_are_charged_to_the_product(self):
        book = replay(facts(transactions=[tx(1, MINERAL, 10, 50.0, buy=True), tx(3, MINERAL, 4, 100.0, buy=False),
                                          tx(3, MINERAL, 6, 100.0, buy=False)],
                            entries=[journal(2, "brokers_fee", -30.0), journal(3, "transaction_tax", -45.0)],
                            orders=[order(1, MINERAL, buy=False, issued=stamp(2), price=100.0, volume=10)]))
        (fee,) = lines(book, "broker_fee")
        self.assertEqual((MINERAL, -30.0), (fee.type_id, fee.amount))
        self.assertEqual([-27.0, -18.0], sorted(e.amount for e in lines(book, "sales_tax")))  # by sale value

    def test_a_fee_for_an_order_modified_later_is_matched_by_the_issuers_rate(self):
        book = replay(facts(entries=[journal(1, "brokers_fee", -10.0),            # measures the 1 % rate
                                     journal(2, "brokers_fee", -99.0)],           # listing later modified
                            orders=[order(1, MINERAL, buy=False, issued=stamp(1), price=10.0, volume=100),
                                    order(2, PART, buy=True, issued=stamp(9), price=99.0, volume=100)]))
        self.assertEqual(1, book.notes["broker_fees_matched_by_rate"])
        self.assertAlmostEqual(99.0, book.pools[("item", PART)].cost)

    def test_a_fee_that_fits_no_order_is_overhead(self):
        book = replay(facts(entries=[journal(2, "brokers_fee", -99.0)]))
        (fee,) = lines(book, "broker_fee")
        self.assertEqual((ledger.SCOPE_OVERHEAD, "broker fee, order not matched"), (fee.scope, fee.label))

    def test_office_rent_is_overhead_and_an_alts_personal_journal_is_not(self):
        book = replay(facts(entries=[journal(1, "office_rental_fee", -25.0),
                                     journal(1, "contract_brokers_fee", -9.0, wallet=f"char:{CHAR}")]))
        self.assertEqual([("office rent", -25.0)], [(e.label, e.amount) for e in lines(book, "overhead")])


class PnlTests(unittest.TestCase):
    def book(self) -> ledger.Book:
        return replay(facts(
            transactions=[tx(1, PART, 50, 10.0, buy=True), tx(8, T2_ITEM, 10, 200.0, buy=False),
                          tx(8, MINERAL, 10, 5.0, buy=False)],
            jobs=[job(1, ledger.INVENTION, blueprint_type=T1_BP, product=T2_BP, runs=1, start=stamp(2),
                      end=stamp(3), successes=1, cost=10.0),
                  job(2, ledger.MANUFACTURING, blueprint_type=T2_BP, product=T2_ITEM, runs=10, start=stamp(4),
                      end=stamp(5), cost=0.0)],
            entries=[journal(1, "office_rental_fee", -100.0)],
            blueprints=[copy(1, T2_BP, 2, 4, seen=stamp(4))], opening={CORE: 1.0, MINERAL: 3.0}))

    def test_invention_lines_are_reported_apart_from_other_trade_and_overhead(self):
        (row,) = ledger.pnl(self.book())
        self.assertAlmostEqual(2000.0, row[ledger.SCOPE_INVENTION]["revenue"])
        self.assertAlmostEqual(50.0, row[ledger.SCOPE_OTHER]["revenue"])
        self.assertAlmostEqual(-30.0, row[ledger.SCOPE_OTHER]["cogs_opening_stock"])
        # 10 units from 49 parts at ME 2 (490), plus the invention's 2 cores and job fee (12)
        self.assertAlmostEqual(-502.0, row[ledger.SCOPE_INVENTION]["cogs"])
        self.assertEqual({"office rent": -100.0}, row["overhead"])
        self.assertAlmostEqual(2050.0 - 502.0 - 30.0 - 100.0, row["net_profit"])

    def test_a_period_filter_and_weekly_rows(self):
        self.assertEqual([], ledger.pnl(self.book(), since=stamp(9)[:10]))
        weeks = ledger.pnl(self.book(), by="week")
        self.assertEqual(["2026-W36", "2026-W37"], [r["period"] for r in weeks])

    def test_products_carry_unit_cost_and_margin(self):
        (item,) = ledger.products(self.book(), scope=ledger.SCOPE_INVENTION)
        self.assertEqual((10, 10), (item["built"], item["sold"]))
        self.assertAlmostEqual(50.2, item["unit_cost"])
        self.assertAlmostEqual(1498.0, item["profit"])


# -- store -------------------------------------------------------------------------------------------

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-ledger-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "ledger.sqlite3")

    def connect(self):
        # Closed before the directory goes: Windows cannot delete a file that is still open.
        conn = ledger_db.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def test_writing_the_same_rows_twice_counts_them_once(self):
        conn = self.connect()
        rows = [{"transaction_id": 1, "date": stamp(1), "type_id": 34, "quantity": 5, "unit_price": 4.0,
                 "is_buy": True}]
        self.assertEqual(1, ledger_db.insert_transactions(conn, WALLET, rows))
        self.assertEqual(0, ledger_db.insert_transactions(conn, WALLET, rows))
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def test_a_jobs_outcome_is_refreshed_when_esi_reports_it(self):
        conn = self.connect()
        row = {"job_id": 7, "activity_id": 8, "runs": 3, "start_date": stamp(1), "status": "active"}
        ledger_db.upsert_jobs(conn, f"corp:{CORP}", [row], stamp(1))
        ledger_db.upsert_jobs(conn, f"corp:{CORP}", [dict(row, status="delivered", successful_runs=2)], stamp(2))
        self.assertEqual(("delivered", 2, stamp(1)), tuple(conn.execute(
            "SELECT status, successful_runs, first_seen FROM jobs").fetchone()))

    def test_read_only_never_creates_a_ledger(self):
        with self.assertRaises(FileNotFoundError):
            ledger_db.connect(self.path, readonly=True)
        self.assertFalse(os.path.exists(self.path))

    def test_a_version_1_ledger_gains_the_log_and_keeps_its_rows(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(ledger_db._SCHEMA_V1)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO meta (key, value) VALUES ('cutover', '2026-09-04')")
        conn.commit()
        conn.close()
        conn = self.connect()
        self.assertEqual(ledger_db.SCHEMA_VERSION, conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual("2026-09-04", ledger_db.cutover(conn))
        with conn:
            ledger_db.add_note(conn, stamp(1), "update", "first")
        self.assertEqual(["first"], [n["title"] for n in ledger_db.notes(conn)])

    def test_only_a_to_do_can_be_closed(self):
        conn = self.connect()
        with conn:
            todo = ledger_db.add_note(conn, stamp(1), "todo", "sell run")
            update = ledger_db.add_note(conn, stamp(2), "update", "all slots busy")
        self.assertEqual("open", ledger_db.note(conn, todo)["status"])
        self.assertEqual("done", ledger_db.close_note(conn, todo, stamp(3))["status"])
        with self.assertRaises(RuntimeError):
            ledger_db.close_note(conn, update, stamp(3))
        with self.assertRaises(RuntimeError):
            ledger_db.add_note(conn, stamp(1), "musing", "not a kind")

    def test_a_ledger_from_a_newer_version_is_refused_rather_than_misread(self):
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version = {ledger_db.SCHEMA_VERSION + 1}")
        conn.close()
        with self.assertRaises(RuntimeError):
            ledger_db.connect(self.path)


# -- sync and commands against the fake ESI --------------------------------------------------------------

CORP_JOBS = [
    {"activity_id": 8, "blueprint_id": 1, "blueprint_location_id": 60003760, "blueprint_type_id": T1_BP,
     "cost": 500.0, "duration": 3600, "end_date": stamp(2, 13), "facility_id": 60003760,
     "installer_id": ADA.character_id, "job_id": 9001, "licensed_runs": 10, "location_id": 60003760,
     "output_location_id": 60003760, "probability": 0.4, "product_type_id": T2_BP, "runs": 2,
     "start_date": stamp(2, 12), "status": "delivered", "successful_runs": 1},
]
CORP_TRANSACTIONS = [
    {"client_id": STRANGER, "date": stamp(1), "is_buy": True, "journal_ref_id": 1, "location_id": 60003760,
     "quantity": 10, "transaction_id": 5001, "type_id": CORE, "unit_price": 100.0},
    {"client_id": STRANGER, "date": stamp(3), "is_buy": False, "journal_ref_id": 2, "location_id": 60003760,
     "quantity": 2, "transaction_id": 5002, "type_id": MINERAL, "unit_price": 50.0},
]
CORP_JOURNAL = [
    {"amount": -500.0, "balance": 1.0, "context_id": 9001, "context_id_type": "industry_job_id",
     "date": stamp(2, 12), "description": "fee", "first_party_id": CORP_SHARED, "id": 8001,
     "ref_type": "researching_technology", "second_party_id": 1000132},
    {"amount": -1000.0, "balance": 1.0, "date": stamp(1), "description": "rent", "first_party_id": CORP_SHARED,
     "id": 8002, "ref_type": "office_rental_fee", "second_party_id": 1000020},
]
# Ada's own book: one sale made on the corporation's behalf (already in its wallet) and one her own.
ADA_TRANSACTIONS = [
    dict(CORP_TRANSACTIONS[1], is_personal=False),
    {"client_id": STRANGER, "date": stamp(4), "is_buy": False, "is_personal": True, "journal_ref_id": 3,
     "location_id": 60003760, "quantity": 1, "transaction_id": 5003, "type_id": MINERAL, "unit_price": 60.0},
]


class LedgerCommandTests(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.addCleanup(self.env.stop)
        self.env.install_core()
        server = self.env.server
        corp = f"/corporations/{CORP_SHARED}"
        server.get(f"{corp}/industry/jobs", token=ADA.token, doc=CORP_JOBS)
        server.get(f"{corp}/orders", token=ADA.token, doc=[])
        server.get(f"{corp}/orders/history", token=ADA.token, doc=[])
        server.get(f"{corp}/blueprints", token=ADA.token, doc=[
            {"item_id": 42, "type_id": T2_BP, "location_id": 60003760, "location_flag": "CorpSAG4",
             "quantity": -2, "runs": 1, "material_efficiency": 2, "time_efficiency": 4}])
        server.get(f"{corp}/wallets/1/journal", token=ADA.token, doc=CORP_JOURNAL)
        server.get(f"{corp}/wallets/1/transactions", token=ADA.token, doc=CORP_TRANSACTIONS)
        for division in range(2, 8):
            server.get(f"{corp}/wallets/{division}/journal", token=ADA.token, doc=[])
            server.get(f"{corp}/wallets/{division}/transactions", token=ADA.token, doc=[])
        me = f"/characters/{ADA.character_id}"
        server.get(f"{me}/industry/jobs", token=ADA.token, doc=[])
        server.get(f"{me}/orders", token=ADA.token, doc=[])
        server.get(f"{me}/orders/history", token=ADA.token, doc=[])
        server.get(f"{me}/blueprints", token=ADA.token, doc=[])
        server.get(f"{me}/wallet/journal", token=ADA.token, doc=[])
        server.get(f"{me}/wallet/transactions", token=ADA.token, doc=ADA_TRANSACTIONS)
        server.get("/markets/prices", doc=[{"type_id": MINERAL, "average_price": 40.0},
                                           {"type_id": CORE, "average_price": 90.0}])
        server.get(f"/markets/{ledger_sync.OPENING_REGION}/history", handler=self._history)
        # Vela is in a corporation too, but consented to nothing the ledger reads.
        self.invention_data = mock.patch("eve_skills.alphadata.blueprint_invention", return_value=INVENTION)
        self.invention_data.start()
        self.addCleanup(self.invention_data.stop)

    @staticmethod
    def _history(call):
        if int(call.query["type_id"]) == MINERAL:
            return [{"date": "2026-08-20", "average": 30.0, "volume": 5},
                    {"date": "2026-08-21", "average": 3000.0, "volume": 1},     # one odd day
                    {"date": "2026-08-22", "average": 32.0, "volume": 5},
                    {"date": "2026-09-20", "average": 99.0, "volume": 5}]      # after the cutover
        return []

    def test_sync_books_every_source_once_and_reports_what_it_could_not_read(self):
        code, out, err = self.env.run(["ledger", "sync", "--json"])
        self.assertEqual(0, code, err)
        report = json.loads(out)
        new = {s["source"]: s["new"] for s in report["sources"]}
        self.assertEqual(1, new[f"corp:{CORP_SHARED}:jobs"])
        self.assertEqual(2, new[f"corp:{CORP_SHARED}:1:transactions"])
        self.assertEqual(1, new[f"char:{ADA.character_id}:transactions"])   # the corp-funded row is dropped
        self.assertEqual("2026-09-01", report["cutover"])
        self.assertTrue(any(p["owner"] == "corp:98000000" and p["code"] == "missing_consent"
                            for p in report["problems"]))
        _code, again, _err = self.env.run(["ledger", "sync", "--json"])
        self.assertEqual(0, sum(s["new"] for s in json.loads(again)["sources"]))

    def test_opening_stock_is_the_median_forge_price_before_the_cutover(self):
        self.env.run(["ledger", "sync"])
        conn = ledger_db.connect()
        self.addCleanup(conn.close)
        self.assertEqual((32.0, "forge_history", "2026-09-01"), tuple(conn.execute(
            "SELECT price, source, basis FROM opening_prices WHERE type_id = ?", (MINERAL,)).fetchone()))
        # No Forge history at all: CCP's average stands in, and says so.
        self.assertEqual("ccp_average", conn.execute(
            "SELECT source FROM opening_prices WHERE type_id = ?", (CORE,)).fetchone()[0])

    def test_pnl_after_a_sync(self):
        self.env.run(["ledger", "sync"])
        code, out, err = self.env.run(["ledger", "pnl", "--json"])
        self.assertEqual(0, code, err)
        (row,) = json.loads(out)["periods"]
        self.assertAlmostEqual(160.0, row["total"]["revenue"])           # 2 x 50 corp + 1 x 60 personal
        self.assertAlmostEqual(60.0, row["revenue_personal_wallets"])
        self.assertAlmostEqual(-96.0, row["total"]["cogs"])              # 3 old units at the 32 median
        self.assertEqual({"office rent": -1000.0}, row["overhead"])
        code, out, _err = self.env.run(["ledger", "pnl"])
        self.assertIn("net profit", out)

    def test_invention_report_after_a_sync(self):
        self.env.run(["ledger", "sync"])
        code, out, err = self.env.run(["ledger", "invention", "--json"])
        self.assertEqual(0, code, err)
        (row,) = json.loads(out)["invention"]
        self.assertEqual(("none", 1, 2), (row["decryptor"], row["successes"], row["attempts"]))
        # 2 attempts x 2 cores at the 100 bought + the 500 journal fee, over one invented run
        self.assertAlmostEqual(900.0, row["cost_per_run"])

    def test_the_watch_hook_syncs_at_most_once_an_hour_and_never_raises(self):
        from eve_skills import esi
        client = esi.Esi("test")
        status, problems = ledger_sync.sync_if_due(client)
        self.assertTrue(status.startswith("ledger: synced"), status)
        self.assertTrue(problems)                                 # Vela's missing consent, as lines
        calls = len(self.env.server.calls)
        status, _problems = ledger_sync.sync_if_due(client)
        self.assertTrue(status.startswith("ledger: last synced"), status)
        self.assertEqual(calls, len(self.env.server.calls))       # nothing asked of ESI
        with mock.patch.object(ledger_sync, "sync", side_effect=sqlite3.OperationalError("locked")):
            status, problems = ledger_sync.sync_if_due(client, interval=0)
        self.assertEqual((None, ["ledger sync failed: locked"]), (status, problems))

    def test_each_sync_records_a_snapshot_that_progress_lists(self):
        self.env.run(["ledger", "sync"])
        code, out, err = self.env.run(["ledger", "progress", "--json"])
        self.assertEqual(0, code, err)
        (snap,) = json.loads(out)["snapshots"]
        self.assertEqual("2026-09-01", snap["pnl_since"])
        self.assertAlmostEqual(160.0, snap["total_revenue"])
        self.assertEqual({}, snap["jobs_running"])
        code, out, _err = self.env.run(["ledger", "progress"])
        self.assertIn("open to-dos (0)", out)

    def test_notes_are_added_listed_shown_and_to_dos_closed(self):
        run = self.env.run
        self.assertEqual(0, run(["ledger", "note", "add", "sell run to Dodixie", "--kind", "todo"])[0])
        self.assertEqual(0, run(["ledger", "note", "add", "labs 26/26", "--body", "all **busy**",
                                 "--at", stamp(2)])[0])
        _code, out, _err = run(["ledger", "note", "list", "--open", "--json"])
        (todo,) = json.loads(out)
        self.assertEqual(("sell run to Dodixie", "open"), (todo["title"], todo["status"]))
        _code, out, _err = run(["ledger", "note", "show", "--last", "2"])
        self.assertIn("all **busy**", out)
        self.assertEqual(0, run(["ledger", "note", "done", str(todo["id"])])[0])
        _code, out, _err = run(["ledger", "note", "list", "--open"])
        self.assertIn("no notes match", out)
        code, _out, err = run(["ledger", "note", "show", "999"])
        self.assertEqual((1, True), (code, "no note 999" in err))

    def test_reports_before_any_sync_say_how_to_start(self):
        code, _out, err = self.env.run(["ledger", "pnl"])
        self.assertEqual(1, code)
        self.assertIn("ledger sync", err)


class DoctorLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-ledger-doctor-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def synced(self, when: str):
        conn = ledger_db.connect()
        with conn:
            ledger_db.set_meta(conn, "last_sync", when)
        conn.close()

    def now(self, day: int) -> float:
        from datetime import datetime
        return datetime.fromisoformat(stamp(day).replace("Z", "+00:00")).timestamp()

    def test_no_ledger_is_fine_and_is_not_created(self):
        check = doctor._check_ledger(self.now(1))
        self.assertEqual(doctor.OK, check["status"])
        self.assertFalse(os.path.exists(ledger_db.db_path(create=False)))

    def test_a_ledger_near_esis_window_warns_and_one_past_it_fails(self):
        self.synced(stamp(1))
        self.assertEqual(doctor.OK, doctor._check_ledger(self.now(5))["status"])
        self.assertEqual(doctor.WARN, doctor._check_ledger(self.now(25))["status"])
        from datetime import datetime, timedelta
        later = (datetime.fromisoformat(stamp(1).replace("Z", "+00:00")) + timedelta(days=31)).timestamp()
        self.assertEqual(doctor.FAIL, doctor._check_ledger(later)["status"])


if __name__ == "__main__":
    unittest.main()
