"""`pi` end to end: the recipe tree and its arithmetic, the colony budget verdict, one planet type's
reachable set, and what the command refuses to answer.

Runs against tests/fake_esi.py's in-process ESI with a synthetic planetary-industry document seeded
into `$XDG_DATA_HOME`, so every quantity, price and customs figure below is written out by hand from
the fixture rows (see the PI universe under this header) rather than read back from the code. What
this file protects is the seams: that quantities scale through the tree instead of being copied per
cycle, that a published reference price stays distinguishable from a buyable ask, that customs bills
both multipliers the document carries and never folds them into the margin, that an over-budget colony
is a printed verdict with exit code 0 while nonsense input is a refusal, that a planet's reachable set
excludes anything needing an import - and that `fit` and `planet-type` send no request at all.

The last class runs `pi fit` against the *shipped* snapshot instead of a fixture, because the numbers
that command exists to produce (15 heads on a level-4 colony, 1 head once four ECUs and a launchpad
have eaten the powergrid) are properties of real SDE data and no synthetic world can stand in for them.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eve_skills import alphadata, cli, esi as esi_mod
from tests.fake_esi import (
    MARKET_BOOK_AGE, MARKET_FORGE, MARKET_IDS, MARKET_PRICES, STATION_JITA, SYSTEM_FORGE,
    FakeEsiEnv, http_date, iso, _order,
)

# ---------------------------------------------------------------------------
# the synthetic planetary world
# ---------------------------------------------------------------------------
# Three planet types, five raw materials, ten schematics and one tier-4 product that no single planet
# can reach - which is the shape the real SDE has too, so the closure test below exercises the rule
# rather than a toy. Planet type ids are real (the document keys by them); everything else is invented.
PI_TEMPERATE, PI_BARREN, PI_LAVA = 11, 2016, 2015

PI_LEAF, PI_RESIN = 950001, 950002        # Temperate: Leaf Fibres, Raw Resin
PI_BASE_ORE, PI_NOBLE_ORE = 950003, 950004    # Barren
PI_MAGMA = 950005                         # Lava

PI_THREAD, PI_SHEET = 950011, 950012      # tier 1 off Temperate
PI_INGOT_R, PI_INGOT_P = 950013, 950014   # tier 1 off Barren
PI_BATCH = 950015                         # tier 1 off Lava

PI_CLOTH = 950021                         # tier 2, Temperate only
PI_PLATE = 950022                         # tier 2, Barren only
PI_LENS = 950023                          # tier 2, wants Thread (Temperate) + Glass Batch (Lava)
PI_FRAME = 950031                         # tier 3, wants Woven Cloth (Temperate) + Alloy Plate (Barren)
PI_RELAY = 950041                         # tier 4, the product the chain test asks for

PI_TAX = {0: 5.0, 1: 400.0, 2: 7200.0, 3: 60000.0, 4: 1200000.0}

PI_NAMES = {PI_LEAF: "Leaf Fibres", PI_RESIN: "Raw Resin", PI_BASE_ORE: "Base Ore",
            PI_NOBLE_ORE: "Noble Ore", PI_MAGMA: "Magma Rock", PI_THREAD: "Thread",
            PI_SHEET: "Resin Sheet", PI_INGOT_R: "Reactive Ingot", PI_INGOT_P: "Precious Ingot",
            PI_BATCH: "Glass Batch", PI_CLOTH: "Woven Cloth", PI_PLATE: "Alloy Plate",
            PI_LENS: "Lens Blank", PI_FRAME: "Composite Frame", PI_RELAY: "Signal Relay"}


def _schematic(key, out_id, out_qty, tier, facility, cycle, planets, inputs):
    return str(key), {"name": f"{PI_NAMES[out_id]} Making", "cycle": cycle,
                      "in": {str(ident): qty for ident, qty in inputs.items()},
                      "out": {str(out_id): out_qty}, "tier": tier, "facility": facility,
                      "planet_types": list(planets)}


# Tier 1 takes 100 raw and yields 20 (a 5x concentration) in half an hour; tier 2 and up take one hour.
PI_SCHEMATICS = dict([
    _schematic(7001, PI_THREAD, 20, 1, "basic", 1800, [PI_TEMPERATE], {PI_LEAF: 100}),
    _schematic(7002, PI_SHEET, 20, 1, "basic", 1800, [PI_TEMPERATE], {PI_RESIN: 100}),
    _schematic(7003, PI_INGOT_R, 20, 1, "basic", 1800, [PI_BARREN], {PI_BASE_ORE: 100}),
    _schematic(7004, PI_INGOT_P, 20, 1, "basic", 1800, [PI_BARREN], {PI_NOBLE_ORE: 100}),
    _schematic(7005, PI_BATCH, 20, 1, "basic", 1800, [PI_LAVA], {PI_MAGMA: 100}),
    _schematic(7011, PI_CLOTH, 10, 2, "advanced", 3600, [PI_TEMPERATE],
               {PI_THREAD: 40, PI_SHEET: 40}),
    _schematic(7012, PI_PLATE, 10, 2, "advanced", 3600, [PI_BARREN], {PI_INGOT_R: 40, PI_INGOT_P: 40}),
    _schematic(7013, PI_LENS, 10, 2, "advanced", 3600, [PI_TEMPERATE, PI_LAVA],
               {PI_THREAD: 40, PI_BATCH: 20}),
    _schematic(7021, PI_FRAME, 5, 3, "advanced", 3600, [PI_TEMPERATE, PI_BARREN],
               {PI_CLOTH: 10, PI_PLATE: 10}),
    _schematic(7031, PI_RELAY, 1, 4, "high_tech", 3600, [PI_TEMPERATE], {PI_FRAME: 5, PI_LENS: 5}),
])

# Every commodity a schematic produces, with the tier and customs value the SDE carries for it - raw
# materials are added below, since nothing refines them.
PI_COMMODITIES = {str(int(next(iter(_row["out"])))): {
    "name": PI_NAMES[int(next(iter(_row["out"])))], "tier": _row["tier"], "tax": PI_TAX[_row["tier"]]}
    for _key, _row in PI_SCHEMATICS.items()}
PI_COMMODITIES.update({str(ident): {"name": PI_NAMES[ident], "tier": 0, "tax": PI_TAX[0]}
                       for ident in (PI_LEAF, PI_RESIN, PI_BASE_ORE, PI_NOBLE_ORE, PI_MAGMA)})


def _structure(key, role, planet, cpu, power, head_cpu=None, head_power=None):
    row = {"name": f"{role.replace('_', ' ').title()} ({planet})", "role": role,
           "planet_type": planet, "cpu": cpu, "power": power}
    if head_cpu is not None:
        row["head_cpu"] = head_cpu
        row["head_power"] = head_power
    return str(key), row


# One structure per role per planet, all at the figures the real SDE gives (extractor 200/800, basic
# 200/800, advanced 500/700, high-tech 1100/400, storage 500/700, launchpad 3600/700, ECU body 400/2600
# with heads at 110/550). They are deliberately identical across the three planets - `fit` is allowed to
# answer without a planet type only while that holds, and `test_command_centres_that_disagree_are_not`
# breaks it on purpose to prove the refusal.
PI_STRUCTURES = {}
for _i, _planet in enumerate((PI_TEMPERATE, PI_BARREN, PI_LAVA)):
    base = 950100 + _i * 10
    PI_STRUCTURES.update([
        _structure(base + 1, "extractor", _planet, 200, 800),
        _structure(base + 2, "command_center", _planet, 0, 0),
        _structure(base + 3, "storage_facility", _planet, 500, 700),
        _structure(base + 4, "launchpad", _planet, 3600, 700),
        _structure(base + 5, "extractor_control_unit", _planet, 400, 2600, 110, 550),
        _structure(base + 6, "processor_basic", _planet, 200, 800),
        _structure(base + 7, "processor_advanced", _planet, 500, 700),
        _structure(base + 8, "processor_high_tech", _planet, 1100, 400),
    ])

# The command centre's output per upgrade level. Round numbers here; the shipped snapshot's measured
# ladder (1675/6000 up to 25415/19000) is pinned by PiShippedSnapshotTestCase below.
PI_CCU_OUTPUT = {str(level): {"type_id": 950102, "cpu": cpu, "power": power}
                 for level, (cpu, power) in enumerate(((1000, 5000), (5000, 8000), (10000, 10000),
                                                       (15000, 12000), (20000, 16000), (25000, 19000)))}

PI_RESOURCES = {str(PI_TEMPERATE): [PI_LEAF, PI_RESIN],
                str(PI_BARREN): [PI_BASE_ORE, PI_NOBLE_ORE],
                str(PI_LAVA): [PI_MAGMA]}

PI_DOCUMENT = {"source": "synthetic", "build": 2500001, "fetched": iso(-86400),
               "planet_types": {str(PI_TEMPERATE): "Temperate", str(PI_BARREN): "Barren",
                                str(PI_LAVA): "Lava"},
               "resources": PI_RESOURCES, "commodities": PI_COMMODITIES,
               "schematics": PI_SCHEMATICS, "structures": PI_STRUCTURES,
               "command_centers": {str(pid): PI_CCU_OUTPUT for pid in (PI_TEMPERATE, PI_BARREN, PI_LAVA)},
               "tax_factors": {"import": 0.5, "export": 1.0}}

# Cheapest ask at Jita for everything the tree needs - except Magma Rock, which nobody orders and only
# ESI's published reference prices (adjusted 6.00), and Signal Relay itself at 5,000.
PI_PRICES_BY_TYPE = {PI_LEAF: 1.0, PI_RESIN: 2.0, PI_BASE_ORE: 3.0, PI_NOBLE_ORE: 4.0,
                     PI_THREAD: 10.0, PI_SHEET: 20.0, PI_INGOT_R: 30.0, PI_INGOT_P: 40.0,
                     PI_BATCH: 50.0, PI_CLOTH: 100.0, PI_PLATE: 200.0, PI_LENS: 300.0,
                     PI_FRAME: 1000.0, PI_RELAY: 5000.0}

PI_ORDERS = [_order(951000 + index, price, STATION_JITA, SYSTEM_FORGE, remain=10_000, type_id=ident)
             for index, (ident, price) in enumerate(sorted(PI_PRICES_BY_TYPE.items()))]

# `/markets/prices` additions: Magma Rock has no order anywhere, so this row - not a zero - is what
# prices it, and the run that needs it is the only one that pays for the megabyte document.
PI_PRICE_ROWS = [{"type_id": PI_MAGMA, "adjusted_price": 6.0}]

PI_IDS = {name: {"inventory_types": [ident]} for ident, name in PI_NAMES.items()}


class PiTestCase(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_market()
        patcher = mock.patch.dict(MARKET_IDS, PI_IDS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_document(PI_DOCUMENT)
        self.seed_orders(PI_ORDERS)
        self.addCleanup(self.env.stop)

    # -- seeding ---------------------------------------------------------------

    def write_document(self, document: dict) -> None:
        self.env._write_json(os.path.join(self.env.data_home, "eve-skills", "planet_industry.json"),
                             document)

    def seed_orders(self, rows: list[dict]) -> None:
        """The regional book plus the PI asks, carrying an `Expires` so the quote cache is licensed to
        remember them - which is what lets a test observe that the second run reads no orders at all."""
        def handler(call):
            region = int(call.path.split("/")[2])
            served = list(rows)
            for key in ("type_id", "location_id", "system_id"):
                if call.query.get(key) is not None:
                    served = [r for r in served if str(r[key]) == call.query[key]]
            return served, {"Last-Modified": http_date(-MARKET_BOOK_AGE[region]),
                            "Expires": http_date(300), "X-Pages": "1"}

        self.env.server.get(f"/markets/{MARKET_FORGE}/orders", handler=handler)
        # Live ESI caches this endpoint, so the route says so too. The transport's copy of it lives as
        # long as the client does, so a run that needs a reference price pays exactly one request for
        # it however many types need one - which is what the caching test below counts.
        self.env.server.get("/markets/prices", doc=MARKET_PRICES + PI_PRICE_ROWS,
                            headers={"Last-Modified": http_date(-60), "Expires": http_date(3600)})

    # -- running ---------------------------------------------------------------

    def json_doc(self, argv: list[str]) -> dict:
        """Run the command in `--json` mode and hand back the parsed document, so a test can assert on
        figures instead of on column widths; a refusal fails here with its stderr as the message."""
        code, out, err = self.env.run([*argv, *(["--json"] if "--json" not in argv else [])])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def step(self, doc: dict, type_id: int, depth: int | None = None) -> dict:
        """The tree's row for a commodity. A shared input appears once per branch - Thread is both a
        frame's and a lens's input - so `depth` names the occurrence when there is more than one."""
        matches = [s for s in doc["steps"] if s["type_id"] == type_id
                   and (depth is None or s["depth"] == depth)]
        self.assertTrue(matches, f"no row for {PI_NAMES[type_id]} at depth {depth}: {doc['steps']}")
        return matches[0]

    def refuses(self, argv: list[str], *fragments: str) -> str:
        code, out, err = self.env.run(argv)
        self.assertEqual(code, 1, f"{argv} should have been refused; stdout={out!r} stderr={err!r}")
        self.assertEqual(out, "")
        for fragment in fragments:
            self.assertIn(fragment, err)
        return err

    # -- pi chain ---------------------------------------------------------------

    def test_the_tree_scales_every_branch_from_one_unit_of_product(self):
        """Quantities are what one Signal Relay needs, not what one facility cycle makes.

        Hand-worked from the fixture: 1 relay wants 5 frames + 5 lenses; a frame (out 5) at 5 wants
        10 cloth + 10 plate, each of which (out 10) wants 40 of its two tier-1s; a lens (out 10) at 5
        is half a cycle, so it wants 20 Thread + 10 Glass Batch - and Glass Batch (out 20) at 10 wants
        50 Magma Rock. Every tier-1 turns 100 raw into 20. Thread appears twice, once per branch, each
        at the quantity that branch needs."""
        doc = self.json_doc(["pi", "chain", "Signal Relay"])
        self.assertEqual(doc["product"], {"type_id": PI_RELAY, "name": "Signal Relay"})
        tree = [(s["name"], s["depth"], s["qty_per_unit_of_product"]) for s in doc["steps"]]
        self.assertEqual(tree, [
            ("Signal Relay", 0, 1),
            ("Composite Frame", 1, 5),
            ("Alloy Plate", 2, 10),
            ("Precious Ingot", 3, 40),
            ("Noble Ore", 4, 200),
            ("Reactive Ingot", 3, 40),
            ("Base Ore", 4, 200),
            ("Woven Cloth", 2, 10),
            ("Resin Sheet", 3, 40),
            ("Raw Resin", 4, 200),
            ("Thread", 3, 40),
            ("Leaf Fibres", 4, 200),
            ("Lens Blank", 1, 5),
            ("Glass Batch", 2, 10),
            ("Magma Rock", 3, 50),
            ("Thread", 2, 20),
            ("Leaf Fibres", 3, 100),
        ])

    def test_the_text_tree_indents_and_names_the_planets_of_every_leaf(self):
        code, out, err = self.env.run(["pi", "chain", "Signal Relay"])
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        relay = next(line for line in lines if line.startswith("Signal Relay"))
        # Indentation is inside the commodity cell, so the columns still line up.
        self.assertTrue(relay.startswith("Signal Relay"), relay)
        cloth = next(line for line in lines if "Woven Cloth" in line)
        self.assertTrue(cloth.startswith("    Woven Cloth"), cloth)
        leaf = next(line for line in lines if "Noble Ore" in line)
        self.assertIn("Barren", leaf)
        self.assertNotIn("Temperate", leaf)
        self.assertIn("advanced", cloth)
        self.assertIn("1h 00m", cloth)
        self.assertIn("30m", next(line for line in lines if "Thread" in line))

    def test_value_added_is_per_cycle_of_that_facility(self):
        """(out x price - in x prices) / cycle hours, hand-worked from PI_PRICES_BY_TYPE:
        Thread  (20x10 - 100x1)/0.5h = 200; Woven Cloth (10x100 - 40x10 - 40x20)/1h = -200;
        Composite Frame (5x1000 - 10x100 - 10x200)/1h = 2000; Signal Relay -1500."""
        doc = self.json_doc(["pi", "chain", "Signal Relay"])
        for type_id, want in ((PI_THREAD, 200.0), (PI_SHEET, 400.0), (PI_CLOTH, -200.0),
                              (PI_PLATE, -800.0), (PI_LENS, 1600.0), (PI_FRAME, 2000.0),
                              (PI_RELAY, -1500.0)):
            self.assertEqual(self.step(doc, type_id)["value_added_per_facility_hour"], want,
                             PI_NAMES[type_id])
        raw = self.step(doc, PI_LEAF)
        self.assertIsNone(raw["value_added_per_facility_hour"])   # extracted: no facility, no cycle
        self.assertEqual(raw["planets"], [{"id": PI_TEMPERATE, "name": "Temperate"}])

    def test_a_type_nobody_orders_is_priced_from_the_published_reference_and_says_so(self):
        """Magma Rock has no ask but an adjusted price of 6.00, so Glass Batch is still computable -
        at (20x50 - 100x6)/0.5h = 800 - and the basis has to stay distinguishable from a buyable ask."""
        doc = self.json_doc(["pi", "chain", "Signal Relay"])
        batch = self.step(doc, PI_BATCH)
        self.assertEqual(batch["price_per_unit"], 50.0)
        self.assertEqual(batch["price_basis"], "min_sell")
        magma = self.step(doc, PI_MAGMA)
        self.assertEqual((magma["price_per_unit"], magma["price_basis"]), (6.0, "esi_adjusted"))
        self.assertEqual(batch["value_added_per_facility_hour"], 800.0)
        code, out, err = self.env.run(["pi", "chain", "Signal Relay"])
        self.assertIn("esi_adjusted", out)

    def test_the_reference_document_is_only_read_when_something_lacks_an_ask(self):
        """/markets/prices is over a megabyte, so a fully quoted tree must not fetch it at all, a tree
        that needs it pays once per run rather than once per type, and the order books - which unlike
        the reference document live in the durable quote cache - are never re-read for a second look."""
        frame = self.json_doc(["pi", "chain", "Composite Frame"])
        self.assertEqual(self.env.server.calls_to("/markets/prices"), [],
                         "a fully priced tree paid for the published reference document")
        self.assertEqual((frame["pricing"]["books_fetched"], frame["pricing"]["books_cached"]),
                         (11, 0))
        relay = self.json_doc(["pi", "chain", "Signal Relay"])
        # Four new commodities - the relay itself, Lens Blank, Glass Batch and Magma Rock - while the
        # eleven the frame's tree already quoted come off the cache.
        self.assertEqual((relay["pricing"]["books_fetched"], relay["pricing"]["books_cached"]), (4, 11))
        self.assertEqual(len(self.env.server.calls_to("/markets/prices")), 1)
        again = self.json_doc(["pi", "chain", "Signal Relay"])
        self.assertEqual((again["pricing"]["books_fetched"], again["pricing"]["books_cached"]), (0, 15))
        self.assertEqual(len(self.env.server.calls_to("/markets/prices")), 2)

    def test_an_unpriceable_input_leaves_its_own_step_blank_rather_than_free(self):
        """Raw Resin with neither an ask nor a published price: its row prints no margin, the note names
        it, and only the steps that need it lose a figure - Woven Cloth's own inputs are still quoted."""
        self.seed_orders([r for r in PI_ORDERS if r["type_id"] != PI_RESIN])
        doc = self.json_doc(["pi", "chain", "Signal Relay"])
        resin = self.step(doc, PI_RESIN)
        self.assertIsNone(resin["price_per_unit"])
        self.assertEqual(resin["unpriced_participants"], [])
        self.assertIsNone(self.step(doc, PI_SHEET)["value_added_per_facility_hour"])
        self.assertEqual(self.step(doc, PI_SHEET)["unpriced_participants"], ["Raw Resin"])
        self.assertEqual(self.step(doc, PI_CLOTH)["value_added_per_facility_hour"], -200.0)
        code, out, err = self.env.run(["pi", "chain", "Signal Relay"])
        self.assertIn("Raw Resin", out)
        self.assertIn("counting them as free", out)

    def test_customs_bills_both_multipliers_the_document_carries(self):
        """At 10%: Signal Relay's inputs are 5 Composite Frame + 5 Lens Blank, so
        (5x60000 + 5x7200) x 0.5 import + 1 x 1200000 x 1.0 export = 1,368,000, and 10% of that is
        136,800 per one-hour cycle. Thread's half-hour cycle bills (100x5)x0.5 + (20x400)x1.0 = 8,250,
        which is 1,650 per facility-hour. Customs never folds into va/fac-hr: both columns stand."""
        doc = self.json_doc(["pi", "chain", "Signal Relay", "--customs-rate", "10"])
        self.assertEqual(doc["customs"], {"rate_pct": 10.0, "import_factor": 0.5, "export_factor": 1.0})
        self.assertEqual(self.step(doc, PI_RELAY)["customs_per_facility_hour"], 136800.0)
        self.assertEqual(self.step(doc, PI_THREAD)["customs_per_facility_hour"], 1650.0)
        self.assertEqual(self.step(doc, PI_LEAF)["customs_per_facility_hour"], None)
        self.assertEqual(self.step(doc, PI_RELAY)["value_added_per_facility_hour"], -1500.0)

    def test_a_customs_rate_outside_its_range_is_refused_before_anything_is_asked(self):
        self.refuses(["pi", "chain", "Signal Relay", "--customs-rate", "150"],
                     "--customs-rate", "0..100")
        self.assertEqual(self.env.server.calls_to(f"/markets/{MARKET_FORGE}/orders"), [])

    def test_hub_and_region_cannot_both_pick_the_scope(self):
        self.refuses(["pi", "chain", "Signal Relay", "--hub", "jita", "--region", "The Forge"],
                     "--hub and --region")
        self.assertEqual(self.env.server.calls_to(f"/markets/{MARKET_FORGE}/orders"), [])

    def test_region_scope_prices_the_whole_book(self):
        """A region scope sends no station filter, so the cheaper ask at the other station in The Forge
        wins: Leaf Fibres is quoted 0.50 there against 1.00 at Jita."""
        self.seed_orders(PI_ORDERS + [_order(951500, 0.50, 60099001, SYSTEM_FORGE, remain=10_000,
                                             type_id=PI_LEAF)])
        station = self.json_doc(["pi", "chain", "Signal Relay"])
        region = self.json_doc(["pi", "chain", "Signal Relay", "--region", "The Forge"])
        self.assertEqual(self.step(station, PI_LEAF)["price_per_unit"], 1.0)
        self.assertEqual(self.step(region, PI_LEAF)["price_per_unit"], 0.5)
        self.assertEqual(region["pricing"]["scope"], "The Forge")
        self.assertIsNone(region["pricing"]["location_id"])

    def test_csv_carries_one_row_per_step_and_notes_go_to_stderr(self):
        code, out, err = self.env.run(["pi", "chain", "Signal Relay", "--customs-rate", "5", "--csv"])
        self.assertEqual(code, 0, err)
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(rows[0], ["depth", "type_id", "name", "tier", "qty_per_unit", "schematic_id",
                                   "facility", "cycle_seconds", "planet_type_ids", "price_per_unit",
                                   "price_basis", "value_added_per_facility_hour", "customs_rate_pct",
                                   "customs_per_facility_hour"])
        self.assertEqual(len(rows), 18)                       # header + the 17 steps
        relay = rows[1]
        self.assertEqual(relay[:5], ["0", str(PI_RELAY), "Signal Relay", "4", "1"])
        self.assertEqual(relay[5:8], ["7031", "high-tech", "3600"])
        self.assertEqual(relay[-2:], ["5.0", "68400"])         # 10% billed half as much at 5%
        leaf = rows[-1]
        self.assertEqual(leaf[1:3], [str(PI_LEAF), "Leaf Fibres"])
        self.assertEqual(leaf[8], str(PI_TEMPERATE))
        self.assertIn("customs/fac-hr", err)
        self.assertNotIn("customs/fac-hr", out)

    # -- pi fit ------------------------------------------------------------------

    def fit(self, *argv: str) -> dict:
        return self.json_doc(["pi", "fit", *argv])

    def test_a_layout_reports_its_load_its_budget_and_the_heads_left_over(self):
        """Level 2 gives 10,000 CPU / 10,000 PG. Two ECUs (400/2600 each) plus a launchpad (3600/700)
        and the asked-for 500/400 of links load 4,900 CPU / 6,300 PG, leaving 5,100 / 3,700 - which at
        110/550 per head is 46 heads on CPU and 6 on powergrid, under the 20-head cap: powergrid binds."""
        doc = self.fit("--ccu", "2", "--ecu", "2", "--launchpad", "1", "--link-allowance", "500,400")
        self.assertEqual(doc["budget"], {"cpu": 10000, "power": 10000, "planet_types_agreeing": 3})
        self.assertEqual(doc["totals"], {"cpu": 4900, "power": 6300})
        self.assertEqual(doc["free"], {"cpu": 5100, "power": 3700})
        heads = doc["heads"]
        self.assertEqual(heads["limits"], {"cpu": 46, "powergrid": 6, "per_ecu_cap": 20})
        self.assertEqual((heads["max_that_fits"], heads["binding"]), (6, "powergrid"))
        self.assertTrue(doc["fits"])
        self.assertFalse(doc["over_budget"])

    def test_the_heads_per_ecu_cap_binds_before_the_budget_does(self):
        """Level 5 leaves plenty of both resources after one ECU, so the game's ten-heads-per-ECU rule is
        what caps the count - and `binding` has to say so rather than naming a resource."""
        doc = self.fit("--ccu", "5", "--ecu", "1")
        self.assertEqual(doc["heads"]["limits"], {"cpu": 223, "powergrid": 29, "per_ecu_cap": 10})
        self.assertEqual((doc["heads"]["max_that_fits"], doc["heads"]["binding"]), (10, "per_ecu_cap"))

    def test_a_cpu_first_world_names_cpu_as_the_binding_resource(self):
        """The real SDE's head figures (110 CPU / 550 PG) make powergrid the tight one at every level, so
        this fixture swaps them to prove the CPU branch is wired and reported - not hardcoded power."""
        swapped = json.loads(json.dumps(PI_DOCUMENT))
        for row in swapped["structures"].values():
            if row["role"] == "extractor_control_unit":
                row["head_cpu"], row["head_power"] = 550, 110
        self.write_document(swapped)
        doc = self.fit("--ccu", "2", "--ecu", "2", "--launchpad", "1", "--link-allowance", "500,400")
        self.assertEqual(doc["heads"]["limits"], {"cpu": 9, "powergrid": 33, "per_ecu_cap": 20})
        self.assertEqual((doc["heads"]["max_that_fits"], doc["heads"]["binding"]), (9, "cpu"))

    def test_being_over_budget_is_a_verdict_and_still_exits_zero(self):
        """The question is "can I add another launchpad?", and answering it with exit 1 would make the
        command useless in a loop that compares layouts."""
        doc = self.fit("--ccu", "0", "--launchpad", "1", "--ecu", "1")
        self.assertEqual(doc["totals"], {"cpu": 4000, "power": 3300})
        self.assertEqual(doc["free"], {"cpu": -3000, "power": 1700})
        self.assertTrue(doc["over_budget"])
        self.assertFalse(doc["fits"])
        self.assertEqual(doc["reasons"], ["CPU over budget by 3,000.00"])
        self.assertEqual(doc["heads"]["max_that_fits"], 0)
        code, out, err = self.env.run(["pi", "fit", "--ccu", "0", "--launchpad", "1", "--ecu", "1"])
        self.assertEqual(code, 0, err)
        self.assertIn("fits: no - CPU over budget by 3,000.00", out)

    def test_heads_that_have_no_ecu_or_exceed_the_cap_are_refused_by_name(self):
        code, out, err = self.env.run(["pi", "fit", "--ccu", "3", "--heads", "4"])
        self.assertEqual(code, 0, err)
        self.assertIn("cannot attach", out)
        self.assertIn("fits: no", out)
        code, out, err = self.env.run(["pi", "fit", "--ccu", "3", "--ecu", "1", "--heads", "12"])
        self.assertEqual(code, 0, err)
        self.assertIn("more heads than the units can carry", out)
        self.assertIn("1 x 10 = 10", out)

    def test_nonsense_input_is_refused(self):
        self.refuses(["pi", "fit", "--ccu", "6"], "command centre's upgrade level", "0, 1, 2, 3, 4, 5")
        self.refuses(["pi", "fit", "--ccu", "2", "--basic", "-1"], "--basic cannot be negative")
        self.refuses(["pi", "fit", "--ccu", "2", "--heads", "-1"], "--heads cannot be negative")
        self.refuses(["pi", "fit", "--ccu", "2", "--link-allowance", "500"],
                     "--link-allowance wants CPU,POWERGRID")
        self.refuses(["pi", "fit", "--ccu", "2", "--link-allowance", "a,b"], "two numbers")

    def test_command_centres_that_disagree_are_not_averaged(self):
        """`fit` has no planet type to choose between them, so a document that gives level 2 different
        output on two planets is refused with both names rather than silently answered with one."""
        split = json.loads(json.dumps(PI_DOCUMENT))
        split["command_centers"][str(PI_LAVA)]["2"] = {"type_id": 950102, "cpu": 11000, "power": 10000}
        self.write_document(split)
        self.refuses(["pi", "fit", "--ccu", "2", "--ecu", "1"],
                     "different output", "Lava", "Temperate")

    def test_a_role_priced_differently_per_planet_is_refused_too(self):
        split = json.loads(json.dumps(PI_DOCUMENT))
        for key, row in split["structures"].items():
            if row["role"] == "processor_advanced" and row["planet_type"] == PI_LAVA:
                row["cpu"] = 600
        self.write_document(split)
        self.refuses(["pi", "fit", "--ccu", "2", "--advanced", "1"],
                     "different fitting costs", "advanced industry facility")

    def test_fit_and_planet_type_send_no_request_at_all(self):
        """Both read only the local snapshot - which is why they take their names from it and work with
        no configuration, no token and no network."""
        for argv in (["pi", "fit", "--ccu", "4", "--ecu", "1"],
                     ["pi", "planet-type", "Barren"],
                     ["pi", "planet-type", "Barren", "--json"]):
            code, out, err = self.env.run(argv)
            self.assertEqual(code, 0, err)
        self.assertEqual(self.env.server.calls, [],
                         f"an offline action reached the network: {self.env.server.calls}")

    # -- pi planet-type -----------------------------------------------------------

    def test_a_planet_lists_only_what_it_can_reach_without_imports(self):
        doc = self.json_doc(["pi", "planet-type", "Temperate"])
        self.assertEqual(doc["planet_type"], {"id": PI_TEMPERATE, "name": "Temperate"})
        self.assertEqual([r["name"] for r in doc["raw_materials"]], ["Leaf Fibres", "Raw Resin"])
        self.assertEqual([p["refines_into"][0]["name"] for p in doc["raw_materials"]],
                         ["Thread", "Resin Sheet"])
        products = {p["name"]: p["tier"] for p in doc["products"]}
        self.assertEqual(products, {"Thread": 1, "Resin Sheet": 1, "Woven Cloth": 2})
        # Lens Blank's facility exists on Temperate and Thread is made here, but its other input is
        # Lava's Glass Batch - so it is absent. Composite Frame wants Barren's Alloy Plate: absent too.
        self.assertNotIn("Lens Blank", products)
        self.assertNotIn("Composite Frame", products)
        cloth = [p for p in doc["products"] if p["name"] == "Woven Cloth"][0]
        self.assertEqual((cloth["facility"], cloth["cycle_seconds"], cloth["schematic_id"]),
                         ("advanced", 3600, 7011))
        self.assertEqual([(i["name"], i["qty"]) for i in cloth["inputs"]],
                         [("Resin Sheet", 40), ("Thread", 40)])

    def test_no_planet_reaches_a_tier_4_product_alone(self):
        """The claim the planning session needed: tier 4 always wants an input from another planet, so
        the note has to print for every one of them - and stop printing the day one does not."""
        for name in ("Temperate", "Barren", "Lava"):
            doc = self.json_doc(["pi", "planet-type", name])
            tiers = {p["tier"] for p in doc["products"]}
            self.assertNotIn(4, tiers, name)
            self.assertFalse(doc["tier4_reachable_anywhere"], name)
            self.assertTrue(any("no tier-4 product is reachable" in w for w in doc["warnings"]), name)

    def test_the_text_output_splits_raw_materials_from_higher_tiers(self):
        code, out, err = self.env.run(["pi", "planet-type", "Barren"])
        self.assertEqual(code, 0, err)
        self.assertIn("raw material  refines into (tier 1)", out)
        self.assertIn("Base Ore      Reactive Ingot", out)
        self.assertIn("Alloy Plate", out)
        self.assertNotIn("Lens Blank", out)          # needs Lava's Glass Batch
        self.assertNotIn("Thread", out)              # Temperate's tier 1 has no business here

    def test_planet_csv_is_one_self_contained_row_per_commodity(self):
        code, out, err = self.env.run(["pi", "planet-type", "Barren", "--csv"])
        self.assertEqual(code, 0, err)
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(rows[0], ["planet_type_id", "planet_type_name", "tier", "type_id", "name",
                                   "facility", "cycle_seconds", "schematic_id", "inputs"])
        by_name = {row[4]: row for row in rows[1:]}
        self.assertEqual(by_name["Base Ore"][2:7], ["0", str(PI_BASE_ORE), "Base Ore", "extractor", ""])
        self.assertEqual(by_name["Reactive Ingot"][2:],
                         ["1", str(PI_INGOT_R), "Reactive Ingot", "basic", "1800", "7003",
                          "Base Ore 100"])
        self.assertEqual(by_name["Alloy Plate"][8], "Precious Ingot 40; Reactive Ingot 40")
        self.assertNotIn("Lens Blank", by_name)
        self.assertIn("no tier-4 product", err)

    def test_an_unknown_planet_names_the_choices(self):
        self.refuses(["pi", "planet-type", "Tundra"], "no planet type named 'Tundra'",
                     "choices: Barren, Lava, Temperate")
        self.refuses(["pi", "planet-type", "9999"], "no planet type with id 9999")
        doc = self.json_doc(["pi", "planet-type", str(PI_LAVA)])
        self.assertEqual(doc["planet_type"], {"id": PI_LAVA, "name": "Lava"})

    # -- the local document's failure modes ---------------------------------------

    def test_a_missing_snapshot_names_the_command_that_installs_it(self):
        """Nothing in the data home and no packaged fallback: the refusal has to name the command that
        writes the file, because `update-data` is the only way to get planetary industry data at all."""
        os.remove(os.path.join(self.env.data_home, "eve-skills", "planet_industry.json"))
        with mock.patch.object(alphadata, "PACKAGE_DATA_DIR", Path(tempfile.mkdtemp())):
            self.refuses(["pi", "planet-type", "Barren"], "no local planetary industry data",
                         "eve-skills update-data")

    def test_an_unusable_snapshot_names_the_command_that_rebuilds_it(self):
        """`alphadata` raises ValueError for a document missing its sections; it must arrive as the same
        one-line `error:` refusal, not as a traceback."""
        self.write_document({"source": "synthetic", "build": 2500001, "fetched": iso(-86400)})
        self.refuses(["pi", "fit", "--ccu", "2", "--ecu", "1"], "not in the expected format",
                     "eve-skills update-data")

    def test_a_recipe_cycle_is_refused_with_the_path_that_loops(self):
        """Without this guard a cyclic document would recurse until the interpreter gave up, and the
        traceback would say nothing about which two schematics point at each other."""
        looped = json.loads(json.dumps(PI_DOCUMENT))
        looped["schematics"]["7001"]["in"] = {str(PI_SHEET): 10}   # Thread wants Resin Sheet...
        looped["schematics"]["7002"]["in"] = {str(PI_THREAD): 10}  # ...which wants Thread back
        self.write_document(looped)
        self.refuses(["pi", "chain", "Signal Relay"], "recipe cycle",
                     "Resin Sheet > Thread > Resin Sheet")

    def test_a_stale_snapshot_warns_but_still_answers(self):
        stale = json.loads(json.dumps(PI_DOCUMENT))
        stale["fetched"] = iso(-200 * 86400)
        self.write_document(stale)
        code, out, err = self.env.run(["pi", "fit", "--ccu", "2", "--ecu", "1"])
        self.assertEqual(code, 0, err)
        self.assertIn("planetary industry data is 200 days old", out)
        self.assertIn("eve-skills update-data", out)

    def test_bare_pi_names_all_three_actions(self):
        self.refuses(["pi"], "pi needs an action", "chain", "fit", "planet-type")


class PiShippedSnapshotTestCase(unittest.TestCase):
    """`pi fit` against the snapshot this package ships, with no fake transport of any kind.

    These four layouts are the ones the planning session actually weighed, and the head counts they
    produce - 15, 18, 1 and 5 - follow from measured SDE figures (level 4 outputs 21,315 CPU / 17,000
    PG, level 5 outputs 25,415 / 19,000; a launchpad draws 3600/700, an ECU 400/2600 with heads at
    110/550). If a future build moves any of them, this class is where the change shows up."""

    def setUp(self):
        # An empty data home so the packaged document is the one read, and a transport that fails
        # loudly: `fit` must not need it.
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-pi-real-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "config"),
            "XDG_CACHE_HOME": os.path.join(self.tmp.name, "cache"),
            "XDG_DATA_HOME": os.path.join(self.tmp.name, "data"),
            "XDG_STATE_HOME": os.path.join(self.tmp.name, "state")})
        patcher.start()
        self.addCleanup(patcher.stop)

        def no_network(*args, **kwargs):
            raise AssertionError("pi fit sent a request; it is meant to read only the local snapshot")

        transport = mock.patch.object(esi_mod.urllib.request, "urlopen", no_network)
        transport.start()
        self.addCleanup(transport.stop)

    def heads(self, argv: list[str]) -> tuple[int, str]:
        """`pi fit --json` for a layout, as (heads that still fit, which limit decided it)."""
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            code = cli.main(["pi", "fit", *argv, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out.getvalue())
        return doc["heads"]["max_that_fits"], doc["heads"]["binding"]

    def test_the_shipped_snapshot_is_the_one_under_test(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli.main(["pi", "fit", "--ccu", "4", "--ecu", "1", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["sde_build"], alphadata.planet_industry()["build"])

    def test_a_two_ecu_colony_at_level_4_fits_15_heads_on_powergrid(self):
        """Load 3600+800+400+500 CPU and 700+5200+1600+700+400 PG leaves 15,515 / 8,400: 141 heads on
        CPU, 15 on powergrid, cap 20 - so powergrid decides."""
        self.assertEqual(self.heads(["--ccu", "4", "--launchpad", "1", "--ecu", "2", "--basic", "2",
                                     "--advanced", "1", "--link-allowance", "500,400"]),
                         (15, "powergrid"))

    def test_the_same_colony_at_level_5_fits_18(self):
        """The upgrade buys 4,085 CPU and 2,000 PG; only the powergrid headroom changes the answer."""
        self.assertEqual(self.heads(["--ccu", "5", "--launchpad", "1", "--ecu", "2", "--basic", "2",
                                     "--advanced", "1", "--link-allowance", "500,400"]),
                         (18, "powergrid"))

    def test_four_ecus_and_a_launchpad_leave_room_for_one_head_at_level_4(self):
        """16,100 of the 17,000 PG is gone before any head is attached: this is the layout where adding
        a fifth ECU buys nothing, and the reason the session capped its colonies at four."""
        self.assertEqual(self.heads(["--ccu", "4", "--launchpad", "1", "--ecu", "4", "--basic", "4",
                                     "--advanced", "2", "--link-allowance", "500,400"]),
                         (1, "powergrid"))

    def test_the_dense_colony_at_level_5_fits_five(self):
        self.assertEqual(self.heads(["--ccu", "5", "--launchpad", "1", "--ecu", "4", "--basic", "4",
                                     "--advanced", "2", "--link-allowance", "500,400"]),
                         (5, "powergrid"))


if __name__ == "__main__":
    unittest.main()
