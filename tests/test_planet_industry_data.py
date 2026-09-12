"""The planetary industry document: which SDE rows survive the join, and what they become.

Types here are synthetic - invented ids sitting in the real PI groups and categories - so a failure
points at the join rather than at whatever CCP shipped this week. The dogma attribute ids, group ids
and category ids below are spelled out as literals on purpose: if a constant in `alphadata` drifts,
the fixture must not drift with it and hide the mistake. Everything is measured against SDE build
3503375. Only the last class reads the packaged snapshot; nothing here touches the network."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from eve_skills import alphadata

# Dogma attributes, as the SDE spells them (build 3503375).
A_POWER_LOAD, A_CPU_LOAD = 15, 49            # what a structure draws from the command center
A_POWER_OUT, A_CPU_OUT = 11, 48              # what a command center supplies
A_HARVEST = 709                              # on an extractor: the resource it pulls
A_RESTRICTION = 1632                         # planetRestriction: the only link to a planet
A_IMPORT_TAX, A_EXPORT_TAX = 1638, 1639      # launchpad / command center customs rates
A_IMPORT_MULT, A_EXPORT_MULT = 1640, 1641    # per-commodity customs multipliers
A_SKILL1_LEVEL = 277                         # requiredSkill1Level: a command center's upgrade level
A_ECU_HEAD_CPU, A_ECU_HEAD_POWER = 1690, 1691
# The ship-fitting cpu/power attributes. No PI type carries either; an ECU is seeded with them so a
# transform that reached for the wrong id would print 30/77 instead of its real fitting cost.
A_SHIP_CPU, A_SHIP_POWER = 50, 30

# Item groups and the categories that say what kind of planetary thing they hold.
G_EXTRACTOR, G_COMMAND_CENTER, G_PROCESSOR = 1026, 1027, 1028
G_STORAGE, G_LAUNCHPAD, G_ECU, G_LINK = 1029, 1030, 1063, 1036
G_RAW, G_TIER1, G_TIER2, G_TIER4 = 1032, 1042, 1034, 1041
C_STRUCTURES, C_RAW, C_COMMODITIES = 41, 42, 43

TEMPERATE, ICE, BARREN, SHATTERED = 11, 12, 2016, 30889


def group(gid: int, category: int) -> dict:
    return {"_key": gid, "categoryID": category}


GROUPS = [
    group(G_EXTRACTOR, C_STRUCTURES), group(G_COMMAND_CENTER, C_STRUCTURES),
    group(G_PROCESSOR, C_STRUCTURES), group(G_STORAGE, C_STRUCTURES),
    group(G_LAUNCHPAD, C_STRUCTURES), group(G_ECU, C_STRUCTURES),
    # Planetary links are in the structures category but draw nothing and fit nowhere.
    group(G_LINK, C_STRUCTURES),
    group(G_RAW, C_RAW), group(G_TIER1, C_COMMODITIES), group(G_TIER2, C_COMMODITIES),
    group(G_TIER4, C_COMMODITIES),
    group(25, 18),          # an unrelated group, to prove the category is what decides
]


def marker(pid: int, name: str) -> dict:
    """A planet type: unpublished, and named only as "Planet (Barren)".

    Measured in both builds shipped here, this is the sole place a planet id is spelled out at all."""
    return {"_key": pid, "name": {"en": f"Planet ({name})"}, "groupID": 7, "published": False}


def item(tid: int, name: str, gid: int, *, published: bool = True) -> dict:
    return {"_key": tid, "name": {"en": name}, "groupID": gid, "published": published}


def dogma(tid: int, attrs: dict) -> dict:
    """A typeDogma row; values are floats because that is all the SDE ever ships."""
    return {"_key": tid, "dogmaAttributes": [{"attributeID": key, "value": float(value)}
                                             for key, value in attrs.items()]}


def processor(tid: int, planet: int, cpu: float, power: float) -> dict:
    return dogma(tid, {A_CPU_LOAD: cpu, A_POWER_LOAD: power, A_RESTRICTION: planet})


TYPES = [
    marker(TEMPERATE, "Temperate"), marker(ICE, "Ice"), marker(BARREN, "Barren"),
    # A planet nothing can mine: it has no PI items, so it must not appear in the document.
    marker(SHATTERED, "Shattered"),
    # Raw resources. 9003 is unpublished but a recipe eats it; 9004 is unpublished and unused.
    item(9001, "Crystalline Components", G_RAW),
    item(9002, "Native Dust", G_RAW),
    item(9003, "Smuggled Ore", G_RAW, published=False),
    item(9004, "Orphan Resource", G_RAW, published=False),
    # Named and referenced, but carrying no customs multiplier at all.
    item(9007, "Untaxed Dust", G_RAW),
    # Manufactured goods, one per tier the fixture exercises.
    item(9101, "Water", G_TIER1),
    item(9201, "Superconductors", G_TIER2),
    item(9401, "Nanites", G_TIER4),
    # Carries customs multipliers like a commodity but sits in an unrelated group - the SDE has nine
    # of these (salvage wrecks and recovered data cores) and they are not PI goods.
    item(9501, "Wrecked Module", 25),
    # Structures: three processor classes, and no high-tech plant on Ice.
    item(8001, "Barren Basic Industry Facility", G_PROCESSOR),
    item(8002, "Temperate Basic Industry Facility", G_PROCESSOR),
    item(8003, "Ice Basic Industry Facility", G_PROCESSOR),
    item(8011, "Barren Advanced Industry Facility", G_PROCESSOR),
    item(8012, "Temperate Advanced Industry Facility", G_PROCESSOR),
    item(8013, "Ice Advanced Industry Facility", G_PROCESSOR),
    item(8021, "Barren High-Tech Production Plant", G_PROCESSOR),
    item(8022, "Temperate High-Tech Production Plant", G_PROCESSOR),
    item(8301, "Barren Component Extractor", G_EXTRACTOR),
    item(8302, "Barren Dust Extractor", G_EXTRACTOR),
    item(8303, "Temperate Component Extractor", G_EXTRACTOR),
    item(8304, "Ice Dust Extractor", G_EXTRACTOR),
    item(8305, "Ice Smuggler's Extractor", G_EXTRACTOR, published=False),
    item(8401, "Barren Extractor Control Unit", G_ECU),
    item(8501, "Barren Launchpad", G_LAUNCHPAD),
    item(8502, "Temperate Launchpad", G_LAUNCHPAD),
    item(8701, "Ice Storage Facility", G_STORAGE),
    item(8601, "Barren Command Center", G_COMMAND_CENTER),
    item(8602, "Barren Command Center Level 4", G_COMMAND_CENTER, published=False),
    item(8603, "Temperate Command Center", G_COMMAND_CENTER),
    item(8801, "Planetary Link", G_LINK),
]

BASIC, ADVANCED, HIGH_TECH = (200.0, 800.0), (500.0, 700.0), (1100.0, 400.0)

DOGMA = [
    processor(8001, BARREN, *BASIC), processor(8002, TEMPERATE, *BASIC), processor(8003, ICE, *BASIC),
    processor(8011, BARREN, *ADVANCED), processor(8012, TEMPERATE, *ADVANCED),
    processor(8013, ICE, *ADVANCED),
    processor(8021, BARREN, *HIGH_TECH), processor(8022, TEMPERATE, *HIGH_TECH),
    dogma(8301, {A_CPU_LOAD: 200.0, A_POWER_LOAD: 800.0, A_RESTRICTION: BARREN, A_HARVEST: 9001}),
    dogma(8302, {A_CPU_LOAD: 200.0, A_POWER_LOAD: 800.0, A_RESTRICTION: BARREN, A_HARVEST: 9002}),
    dogma(8303, {A_CPU_LOAD: 200.0, A_POWER_LOAD: 800.0, A_RESTRICTION: TEMPERATE, A_HARVEST: 9001}),
    dogma(8304, {A_CPU_LOAD: 200.0, A_POWER_LOAD: 800.0, A_RESTRICTION: ICE, A_HARVEST: 9002}),
    # Unpublished, and it pulls a resource nothing else offers: if it leaked in, Ice would look richer.
    dogma(8305, {A_CPU_LOAD: 200.0, A_POWER_LOAD: 800.0, A_RESTRICTION: ICE, A_HARVEST: 9003}),
    # The ECU carries the ship-fitting attributes as decoys alongside its real body and head costs.
    dogma(8401, {A_CPU_LOAD: 400.0, A_POWER_LOAD: 2600.0, A_RESTRICTION: BARREN,
                 A_ECU_HEAD_CPU: 110.0, A_ECU_HEAD_POWER: 550.0,
                 A_SHIP_CPU: 30.0, A_SHIP_POWER: 77.0}),
    dogma(8501, {A_CPU_LOAD: 3600.0, A_POWER_LOAD: 700.0, A_RESTRICTION: BARREN,
                 A_IMPORT_TAX: 0.5, A_EXPORT_TAX: 1.0}),
    dogma(8502, {A_CPU_LOAD: 3600.0, A_POWER_LOAD: 700.0, A_RESTRICTION: TEMPERATE,
                 A_IMPORT_TAX: 0.5, A_EXPORT_TAX: 1.0}),
    dogma(8701, {A_CPU_LOAD: 500.0, A_POWER_LOAD: 700.0, A_RESTRICTION: ICE}),
    # The base command center supplies resources and carries exportTax 3.0, a modifier on this
    # structure - measured in the SDE - which is not the planet's customs rate.
    dogma(8601, {A_CPU_OUT: 1675.0, A_POWER_OUT: 6000.0, A_RESTRICTION: BARREN, A_EXPORT_TAX: 3.0}),
    dogma(8602, {A_CPU_OUT: 21315.0, A_POWER_OUT: 17000.0, A_RESTRICTION: BARREN,
                 A_SKILL1_LEVEL: 4.0}),
    dogma(8603, {A_CPU_OUT: 1675.0, A_POWER_OUT: 6000.0, A_RESTRICTION: TEMPERATE}),
    dogma(8801, {A_RESTRICTION: BARREN}),
    # Customs multipliers, equal on both directions as measured for all 83 commodities - except 9201,
    # which ships only the export one and has to fall back to it.
    dogma(9001, {A_IMPORT_MULT: 5.0, A_EXPORT_MULT: 5.0}),
    dogma(9002, {A_IMPORT_MULT: 5.0, A_EXPORT_MULT: 5.0}),
    dogma(9003, {A_IMPORT_MULT: 5.0, A_EXPORT_MULT: 5.0}),
    dogma(9004, {A_IMPORT_MULT: 5.0, A_EXPORT_MULT: 5.0}),
    dogma(9101, {A_IMPORT_MULT: 400.0, A_EXPORT_MULT: 400.0}),
    dogma(9201, {A_EXPORT_MULT: 7200.0}),
    dogma(9401, {A_IMPORT_MULT: 1200000.0, A_EXPORT_MULT: 1200000.0}),
    dogma(9501, {A_IMPORT_MULT: 12345.0, A_EXPORT_MULT: 12345.0}),
]


def recipe(sid: int, name: str | None, cycle: int, pins, inputs, outputs) -> dict:
    """A planetSchematics row in the SDE's own shape: flat pin ids, quantities under `types`."""
    doc = {"_key": sid, "cycleTime": cycle, "pins": list(pins),
           "types": [{"_key": tid, "isInput": True, "quantity": qty} for tid, qty in inputs]
                    + [{"_key": tid, "isInput": False, "quantity": qty} for tid, qty in outputs]}
    if name is not None:
        doc["name"] = {"en": name}
    return doc


SCHEMATICS = [
    recipe(121, "Water", 1800, (8001, 8002, 8003), [(9001, 3000)], [(9101, 1)]),
    recipe(65, "Superconductors", 3600, (8011, 8012, 8013), [(9101, 40)], [(9201, 5)]),
    recipe(112, "Organic Mortar Applicators", 3600, (8021, 8022), [(9101, 6), (9003, 40)], [(9401, 1)]),
    # Rows that cannot become a recipe: no name to show, an output nothing tiers, and no plant to run it.
    recipe(999, None, 1800, (8001,), [(9001, 10)], [(9101, 1)]),
    recipe(998, "Salvage Wash", 1800, (8001,), [(9001, 10)], [(9501, 1)]),
    recipe(997, "Barely A Plan", 1800, (), [(9001, 10)], [(9007, 1)]),
]


def transform(schematics=None, dogma_rows=None, types=None, groups=None) -> dict:
    return alphadata._transform_planet_industry(
        SCHEMATICS if schematics is None else schematics,
        DOGMA if dogma_rows is None else dogma_rows,
        TYPES if types is None else types,
        GROUPS if groups is None else groups,
    )


DOC = transform()


class RecipeJoinTests(unittest.TestCase):
    """What a planetSchematics row becomes once pins, dogma, types and groups are joined."""

    def test_a_recipe_carries_its_tier_plant_class_and_planets(self):
        self.assertEqual({
            "121": {"name": "Water", "cycle": 1800, "in": {"9001": 3000}, "out": {"9101": 1},
                    "tier": 1, "facility": "basic",
                    "planet_types": [TEMPERATE, ICE, BARREN]},
            "65": {"name": "Superconductors", "cycle": 3600, "in": {"9101": 40}, "out": {"9201": 5},
                   "tier": 2, "facility": "advanced",
                   "planet_types": [TEMPERATE, ICE, BARREN]},
            # Ice has no high-tech plant, so the tier-4 recipe is not runnable there.
            "112": {"name": "Organic Mortar Applicators", "cycle": 3600,
                    "in": {"9003": 40, "9101": 6}, "out": {"9401": 1},
                    "tier": 4, "facility": "high_tech", "planet_types": [TEMPERATE, BARREN]},
        }, DOC["schematics"])

    def test_a_good_on_both_sides_of_a_recipe_keeps_both_quantities(self):
        """Measured across both builds, no recipe lists the same type twice - but the SDE row shape
        distinguishes the sides with a flag rather than by position, so a transform that keyed by type
        alone would silently drop one side of such a line."""
        loop = [recipe(502, "Reclaimed Water", 1800, (8001,), [(9001, 100), (9101, 4)], [(9101, 1)])]
        row = transform(schematics=loop)["schematics"]["502"]
        self.assertEqual({"9001": 100, "9101": 4}, row["in"])
        self.assertEqual({"9101": 1}, row["out"])

    def test_the_cheapest_pin_decides_the_plant_class(self):
        """A plant is sized by what it needs, so a recipe that can be run on a basic facility must
        not be quoted at the cost of an advanced one - even if CCP ever lists both as pins."""
        mixed = [recipe(500, "Hybrid Line", 1800, (8011, 8001), [(9001, 10)], [(9101, 1)])]
        self.assertEqual("basic", transform(schematics=mixed)["schematics"]["500"]["facility"])

    def test_a_row_that_cannot_be_placed_in_the_chain_is_dropped(self):
        for sid in ("999", "998", "997"):
            self.assertNotIn(sid, DOC["schematics"], f"schematic {sid} should not have survived")

    def test_planet_types_come_from_the_pins_never_from_a_hard_coded_eight(self):
        """Take one processor away and the recipes that needed it lose exactly that planet."""
        types = [t for t in TYPES if t["_key"] != 8022]      # no Temperate high-tech plant either
        doc = transform(types=types)
        self.assertEqual([BARREN], doc["schematics"]["112"]["planet_types"])


class CommodityTests(unittest.TestCase):
    def test_tier_and_customs_multiplier_come_from_the_group_and_dogma(self):
        self.assertEqual({"name": "Crystalline Components", "tier": 0, "tax": 5.0},
                         DOC["commodities"]["9001"])
        self.assertEqual({"name": "Water", "tier": 1, "tax": 400.0}, DOC["commodities"]["9101"])
        self.assertEqual({"name": "Nanites", "tier": 4, "tax": 1200000.0}, DOC["commodities"]["9401"])

    def test_a_commodity_missing_the_import_multiplier_falls_back_to_export(self):
        """Measured equal on all 83 commodities, so the fallback is invisible today - but a type that
        ships only one of the two must still be taxable rather than silently free."""
        self.assertEqual(7200.0, DOC["commodities"]["9201"]["tax"])

    def test_a_tax_carrier_outside_the_commodity_categories_is_not_a_commodity(self):
        """Salvage wrecks and recovered data cores carry the same multipliers and are not PI goods."""
        self.assertNotIn("9501", DOC["commodities"])

    def test_an_unpublished_commodity_survives_only_when_a_recipe_uses_it(self):
        """Every commodity in the shipped builds is published, so this is the narrow exception that
        keeps a hidden input nameable without shipping CCP's scratch rows."""
        self.assertIn("9003", DOC["commodities"])       # eaten by recipe 112
        self.assertNotIn("9004", DOC["commodities"])    # unpublished and referenced nowhere

    def test_a_commodity_with_no_multiplier_is_not_shipped_as_free(self):
        """Dropping it means "there is no customs answer for this good"; pricing it at zero would make
        a bill look cheap rather than unknown."""
        blend = [recipe(501, "Untaxed Blend", 1800, (8001,), [(9007, 3)], [(9101, 1)])]
        doc = transform(schematics=blend)
        self.assertIn("501", doc["schematics"])         # the recipe still runs
        self.assertNotIn("9007", doc["commodities"])    # but the good cannot be taxed


class StructureTests(unittest.TestCase):
    def test_a_structure_carries_its_role_planet_and_fitting_cost(self):
        self.assertEqual({"name": "Barren Basic Industry Facility", "role": "processor_basic",
                          "planet_type": BARREN, "cpu": 200.0, "power": 800.0},
                         DOC["structures"]["8001"])
        self.assertEqual({"name": "Ice Storage Facility", "role": "storage_facility",
                          "planet_type": ICE, "cpu": 500.0, "power": 700.0},
                         DOC["structures"]["8701"])

    def test_the_three_processor_classes_split_on_fitting_cost(self):
        """The classes are named after their rank in the cpu ladder, so each class must report one
        cost and no class may borrow another's - that is what makes "basic" mean something."""
        by_class = {}
        for row in DOC["structures"].values():
            if row["role"].startswith("processor"):
                by_class.setdefault(row["role"], set()).add((row["cpu"], row["power"]))
        self.assertEqual({"processor_basic": {(200.0, 800.0)},
                          "processor_advanced": {(500.0, 700.0)},
                          "processor_high_tech": {(1100.0, 400.0)}}, by_class)

    def test_only_an_ecu_carries_head_costs(self):
        """The head attributes exist on exactly the eight ECUs; a launchpad row with head figures
        would be invented, and an ECU without them could not be fitted at all."""
        ecu = DOC["structures"]["8401"]
        self.assertEqual({"name": "Barren Extractor Control Unit", "role": "extractor_control_unit",
                          "planet_type": BARREN, "cpu": 400.0, "power": 2600.0,
                          "head_cpu": 110.0, "head_power": 550.0}, ecu)
        self.assertNotIn("head_cpu", DOC["structures"]["8501"])

    def test_the_ship_fitting_attributes_are_never_read(self):
        """cpu(50)/power(30) are the ship-fitting pair; measured, zero PI structures carry them. The
        ECU is seeded with them at 30/77, so any row reporting those numbers means the transform
        reached for the wrong attribute and every fit it prints would be wrong."""
        quoted = {(row["cpu"], row["power"]) for row in DOC["structures"].values()}
        self.assertNotIn((30.0, 77.0), quoted)

    def test_an_unpublished_structure_is_not_offered(self):
        self.assertNotIn("8305", DOC["structures"])

    def test_a_planetary_link_gets_no_role_and_no_place_in_a_fit(self):
        self.assertNotIn("8801", DOC["structures"])


class CommandCenterTests(unittest.TestCase):
    def test_levels_are_keyed_by_level_and_carry_the_resources_they_supply(self):
        """cpu/power on a command center row are its output, because that is the number a fit check
        compares the installation's draw against."""
        self.assertEqual({"0": {"type_id": 8601, "cpu": 1675.0, "power": 6000.0},
                          "4": {"type_id": 8602, "cpu": 21315.0, "power": 17000.0}},
                         DOC["command_centers"][str(BARREN)])

    def test_the_base_unit_is_level_zero_because_it_names_no_skill_level(self):
        """Measured: the eight base command centers carry no requiredSkill1Level at all, so level 0 is
        the absence of an upgrade, not a row that happens to say zero."""
        base = next(row for row in DOGMA if row["_key"] == 8601)
        self.assertNotIn(A_SKILL1_LEVEL, [a["attributeID"] for a in base["dogmaAttributes"]])
        self.assertEqual(8601, DOC["command_centers"][str(BARREN)]["0"]["type_id"])

    def test_an_unpublished_upgrade_is_still_a_level_you_can_fit(self):
        """Measured: all forty upgraded command centers are published=false in the SDE. Filtering on
        published would leave one level per planet and no upgrade path to price."""
        self.assertIn("4", DOC["command_centers"][str(BARREN)])

    def test_only_planets_with_items_get_a_command_center_entry(self):
        self.assertEqual({str(TEMPERATE), str(BARREN)}, set(DOC["command_centers"]))


class PlanetTests(unittest.TestCase):
    def test_planet_names_lose_the_marker_wrapper(self):
        self.assertEqual({str(TEMPERATE): "Temperate", str(ICE): "Ice", str(BARREN): "Barren"},
                         DOC["planet_types"])

    def test_a_planet_with_no_planetary_industry_is_absent(self):
        """Shattered exists as a type and has no PI items; `pi` must not offer it as a choice."""
        self.assertNotIn(str(SHATTERED), DOC["planet_types"])

    def test_resources_are_what_the_extractors_pull(self):
        self.assertEqual({str(TEMPERATE): [9001], str(ICE): [9002], str(BARREN): [9001, 9002]},
                         DOC["resources"])


class CustomsTaxTests(unittest.TestCase):
    def test_the_launchpad_rates_are_the_planets_customs_factors(self):
        self.assertEqual({"import": 0.5, "export": 1.0}, DOC["tax_factors"])

    def test_a_command_center_export_tax_is_not_the_planet_rate(self):
        """The command center carries exportTax 3.0 - a modifier on that structure. Reading it as the
        planet's factor would triple every customs bill `pi` quotes."""
        self.assertEqual(1.0, DOC["tax_factors"]["export"])


class DocumentShapeTests(unittest.TestCase):
    def test_the_document_survives_a_json_round_trip_unchanged(self):
        """Every key has to be a string - ids and command center levels alike - or the document that
        lands on disk is not the one the loader hands back."""
        self.assertEqual(DOC, json.loads(json.dumps(DOC)))

    def test_every_section_is_present_even_when_empty(self):
        """A consumer must be able to index by section without guessing what this build shipped."""
        doc = transform(schematics=[], dogma_rows=[], types=[], groups=[])
        self.assertEqual(list(alphadata.PI_SECTIONS), list(doc))


class PackagedDocumentTests(unittest.TestCase):
    """The document that ships in the wheel, and the loader's contract about what is on disk."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="eve-skills-pi-")
        self.addCleanup(tmp.cleanup)
        self.data_dir = os.path.join(tmp.name, "data", "eve-skills")
        os.makedirs(self.data_dir)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.join(tmp.name, "data")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_document(self, document: dict) -> str:
        path = os.path.join(self.data_dir, "planet_industry.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(document, fh)
        return path

    def test_the_packaged_document_answers_from_itself(self):
        """Invariants over the real snapshot: a schematic must never name a good the document cannot
        describe, and every fit figure must belong to a planet the document knows."""
        doc = alphadata.planet_industry()
        self.assertEqual(list(alphadata.PI_SECTIONS), [k for k in alphadata.PI_SECTIONS if k in doc])
        self.assertTrue(doc["planet_types"] and doc["schematics"] and doc["commodities"])
        goods, planets = set(doc["commodities"]), set(doc["planet_types"])
        for schematic in doc["schematics"].values():
            self.assertTrue({str(pid) for pid in schematic["planet_types"]} <= planets,
                            "a recipe offered a planet the document does not name")
        for row in doc["structures"].values():
            self.assertIn(str(row["planet_type"]), planets)
        for levels in doc["command_centers"].values():
            self.assertIn("0", levels, "a planet type shipped without its base command center")
            for level in levels.values():
                self.assertIn(str(level["type_id"]), doc["structures"])

    def test_a_user_copy_of_the_document_wins_over_the_packaged_snapshot(self):
        """Same precedence as every other SDE document: `update-data` output beats what shipped."""
        user = {section: {} for section in alphadata.PI_SECTIONS} | {"build": 2500001}
        self.write_document(user)
        self.assertEqual(2500001, alphadata.planet_industry()["build"])

    def test_a_document_in_the_wrong_shape_names_the_one_command_that_fixes_it(self):
        """Three ways the file on disk stops being this document, each of them fatal to an answer."""
        complete = {section: {} for section in alphadata.PI_SECTIONS}
        cases = [
            {"planet_types": []},                                      # a section that is not a mapping
            {k: v for k, v in complete.items() if k != "tax_factors"},  # a section missing
            complete | {"schematics": {"1": "not-a-row"}},              # a recipe that is not a recipe
        ]
        for wrong in cases:
            self.write_document(wrong)
            with self.assertRaises(ValueError) as caught:
                alphadata.planet_industry()
            self.assertIn("update-data", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
