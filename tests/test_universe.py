"""The type catalogue and the location resolver: request pattern, cache merge, degradation.

Runs against tests/fake_esi.py's in-process ESI with routes registered one exact path at a time -
an unregistered path fails the test loudly - which is what turns "a warm run asks for nothing" into
an assertion instead of a comment. The universe here is invented, like the rest of the fixtures:
four types under three groups under three categories, plus one citadel nobody may look at.

Item ids are synthetic too, and deliberately straddle the int32 boundary that decides whether an id
may enter a `/universe/names` batch at all: live ESI 400s the entire request for one oversized id,
so every test below pins that no item-derived id is ever sent."""

from __future__ import annotations

import itertools
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from eve_skills import esi, sso, universe

from tests.fake_esi import (
    ADA, CORP_SHARED, STATION_JITA, SYSTEM_FORGE, FakeEsiEnv,
)

# --- invented catalogue ----------------------------------------------------
# 34/36 share a group on purpose: one `/universe/groups/18` request has to serve both.
TYPE_ORE = 34
TYPE_INGOT = 36
TYPE_SHIP = 587
TYPE_MODULE = 1234
GROUP_ORES, GROUP_FRIGATES, GROUP_MODULES = 18, 420, 592
CATEGORY_MATERIAL, CATEGORY_SHIP, CATEGORY_MODULE = 5, 7, 8

TYPES = {
    # Tritanium and Pyerite omit `packaged_volume`, which ESI documents as defaulting to `volume`.
    TYPE_ORE: {"type_id": TYPE_ORE, "name": "Tritanium", "group_id": GROUP_ORES, "volume": 0.02},
    TYPE_INGOT: {"type_id": TYPE_INGOT, "name": "Pyerite", "group_id": GROUP_ORES, "volume": 0.02},
    TYPE_SHIP: {"type_id": TYPE_SHIP, "name": "Rifter", "group_id": GROUP_FRIGATES,
                "volume": 2500.0, "packaged_volume": 780.0},
    TYPE_MODULE: {"type_id": TYPE_MODULE, "name": "Sensor Booster I", "group_id": GROUP_MODULES,
                  "volume": 0.5},
}
GROUPS = {
    GROUP_ORES: {"group_id": GROUP_ORES, "name": "Ore", "category_id": CATEGORY_MATERIAL},
    GROUP_FRIGATES: {"group_id": GROUP_FRIGATES, "name": "Frigate", "category_id": CATEGORY_SHIP},
    GROUP_MODULES: {"group_id": GROUP_MODULES, "name": "Midfield", "category_id": CATEGORY_MODULE},
}
CATEGORIES = {CATEGORY_MATERIAL: "Material", CATEGORY_SHIP: "Ship", CATEGORY_MODULE: "Module"}

# Above int32: an item-sized location id, i.e. a player structure or an item, never a station.
CITADEL = 1048236548577
SHIP_ITEM, DRUM_ITEM, MODULE_ITEM, FITTED_ITEM = 90000001, 90000002, 90000003, 90000004
ORPHAN_PARENT = 987654321        # a parent these rows do not include; small enough to name

CUSTOM_NAMES = {SHIP_ITEM: "Nightwatch", DRUM_ITEM: "Ammo Drum"}


def asset_row(item_id: int, type_id: int, location_id: int, *, singleton: bool = False,
              location_type: str = "station", flag: str = "Hangar") -> dict:
    """One asset row with every key live ESI sends for a location decision."""
    return {"item_id": item_id, "type_id": type_id, "quantity": 1, "is_singleton": singleton,
            "location_id": location_id, "location_flag": flag, "location_type": location_type}


def jwt(claims: dict) -> str:
    """A structurally valid unsigned JWT; `sso.decode_jwt` reads the payload for real."""
    return ".".join([sso._b64url(b'{"alg":"none"}'), sso._b64url(json.dumps(claims).encode()), ""])


class UniverseTestCase(unittest.TestCase):
    """Fake ESI, a temp XDG tree, and the catalogue routes registered one id at a time."""

    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_core()          # registers /universe/names over env.names
        self.addCleanup(self.env.stop)
        self.server = self.env.server
        self.client = esi.Esi("unittest")
        for type_id, doc in TYPES.items():
            self.server.get(f"/universe/types/{type_id}", doc=doc)
        for group_id, doc in GROUPS.items():
            self.server.get(f"/universe/groups/{group_id}", doc=doc)
        for category_id, name in CATEGORIES.items():
            self.server.get(f"/universe/categories/{category_id}",
                            doc={"category_id": category_id, "name": name, "published": True})
        # Every location test may describe a named item; tests that want the call refused re-register
        # it with an error. Serving it by default keeps "ESI refused" separate from "we never asked".
        self.asset_names_route(token=ADA.token)

    # -- driving -------------------------------------------------------------

    def resolve(self, rows: list[dict], **kw):
        """`resolve_locations` with a character id supplied, as wave 2 will call it.

        Ada's stored token in this fixture is an opaque string rather than a JWT, so the owner id
        cannot be read out of it; `test_owner_comes_from_the_token_...` covers that path."""
        kw.setdefault("character_id", ADA.character_id)
        return universe.resolve_locations(self.client, rows, **kw)

    def asset_names_route(self, *, token: str, table: dict[int, str] | None = None,
                          error: tuple | None = None, path: str | None = None) -> str:
        """Serve one assets/names endpoint; the recorded calls show which ids were asked about."""
        target = path or f"/characters/{ADA.character_id}/assets/names"
        names = CUSTOM_NAMES if table is None else table

        def handler(call):
            return [{"item_id": i, "name": names[i]} for i in call.json if i in names]

        self.server.post(target, handler=handler, token=token, error=error)
        return target

    # -- assertions over the run -------------------------------------------

    def paths(self) -> list[str]:
        return [call.path for call in self.server.calls]

    def paths_under(self, prefix: str) -> list[str]:
        return [path for path in self.paths() if path.startswith(prefix)]

    def waves(self) -> list[set[str]]:
        """The requested paths grouped into consecutive same-endpoint waves.

        `get_many` fans out on eight threads, so the order *within* a wave is not defined; what is
        defined - and what the catalogue depends on - is that no group is asked for before the types
        that name it, and no category before the groups."""
        out: list[tuple[str, set[str]]] = []
        for path in self.paths():
            kind = path.split("/")[2]
            if not out or out[-1][0] != kind:
                out.append((kind, set()))
            out[-1][1].add(path)
        return [requested for _, requested in out]

    def doc(self) -> dict:
        with open(universe.doc_path(), encoding="utf-8") as fh:
            return json.load(fh)

    def write_doc(self, payload) -> None:
        path = universe.doc_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))


class TypeCatalogueTests(UniverseTestCase):
    def test_cold_run_fetches_types_then_groups_then_categories(self):
        infos = universe.type_info(self.client, [TYPE_ORE, TYPE_INGOT, TYPE_SHIP])

        self.assertEqual(infos[TYPE_ORE], universe.TypeInfo(
            type_id=TYPE_ORE, name="Tritanium", group_id=GROUP_ORES, group_name="Ore",
            category_id=CATEGORY_MATERIAL, category_name="Material",
            volume=0.02, packaged_volume=0.02))
        # A type that does ship `packaged_volume` keeps its own number: on a real hull the two differ
        # by a factor of three, and freight maths has to use the packaged one.
        self.assertEqual((infos[TYPE_SHIP].volume, infos[TYPE_SHIP].packaged_volume),
                         (2500.0, 780.0))
        self.assertEqual(infos[TYPE_SHIP].category_name, "Ship")

        waves = self.waves()
        self.assertEqual(waves[0],
                         {f"/universe/types/{i}" for i in (TYPE_ORE, TYPE_INGOT, TYPE_SHIP)})
        self.assertEqual(waves[1], {f"/universe/groups/{GROUP_ORES}",
                                    f"/universe/groups/{GROUP_FRIGATES}"})
        self.assertEqual(waves[2], {f"/universe/categories/{CATEGORY_MATERIAL}",
                                    f"/universe/categories/{CATEGORY_SHIP}"})
        self.assertEqual(len(waves), 3)
        # Two types, one group request: the fan-out dedupes instead of asking once per row.
        self.assertEqual(len(self.server.calls_to(f"/universe/groups/{GROUP_ORES}")), 1)

    def test_warm_run_makes_no_request_at_all(self):
        first = universe.type_info(self.client, [TYPE_ORE, TYPE_SHIP])
        self.assertTrue(self.server.calls)
        self.server.calls.clear()

        self.assertEqual(universe.type_info(self.client, [TYPE_ORE, TYPE_SHIP]), first)
        self.assertEqual(self.paths(), [])

    def test_nothing_new_rewrites_nothing(self):
        universe.type_info(self.client, [TYPE_ORE])
        mtime = os.stat(universe.doc_path()).st_mtime_ns
        universe.type_info(self.client, [TYPE_ORE])
        self.assertEqual(os.stat(universe.doc_path()).st_mtime_ns, mtime)

    def test_empty_input_touches_neither_disk_nor_network(self):
        self.assertEqual(universe.type_info(self.client, []), {})
        self.assertEqual(self.paths(), [])
        self.assertFalse(os.path.exists(universe.doc_path()))

    def test_document_is_versioned_and_split_by_kind(self):
        universe.type_info(self.client, [TYPE_ORE])
        doc = self.doc()
        self.assertEqual(doc["version"], universe.CACHE_VERSION)
        self.assertEqual(list(doc), ["version", "types", "groups", "categories"])
        self.assertEqual(doc["types"][str(TYPE_ORE)],
                         {"name": "Tritanium", "group_id": GROUP_ORES, "volume": 0.02,
                          "packaged_volume": 0.02})

    def test_refused_type_degrades_and_is_asked_again_next_run(self):
        missing = 999999999
        self.server.get(f"/universe/types/{missing}", error=(404, {"error": "Type not found"}))
        infos = universe.type_info(self.client, [TYPE_ORE, missing])

        self.assertEqual(infos[missing], universe.TypeInfo(
            type_id=missing, name=f"type {missing}", group_id=None, group_name="unknown",
            category_id=None, category_name="unknown", volume=None, packaged_volume=None))
        self.assertEqual(infos[TYPE_ORE].name, "Tritanium")      # one bad id sinks nothing
        self.assertNotIn(str(missing), self.doc()["types"])

        self.server.calls.clear()
        universe.type_info(self.client, [TYPE_ORE, missing])
        self.assertEqual(self.paths(), [f"/universe/types/{missing}"])

    def test_refused_group_keeps_the_type_and_heals_without_refetching_it(self):
        self.server.get(f"/universe/groups/{GROUP_ORES}", error=(500, {"error": "boom"}))
        infos = universe.type_info(self.client, [TYPE_ORE])
        self.assertEqual((infos[TYPE_ORE].name, infos[TYPE_ORE].group_name), ("Tritanium", "unknown"))
        self.assertEqual(infos[TYPE_ORE].category_name, "unknown")
        doc = self.doc()
        self.assertIn(str(TYPE_ORE), doc["types"])               # the type itself is worth keeping
        self.assertEqual(doc["groups"], {})

        self.server.get(f"/universe/groups/{GROUP_ORES}", doc=GROUPS[GROUP_ORES])
        self.server.calls.clear()
        healed = universe.type_info(self.client, [TYPE_ORE])[TYPE_ORE]
        self.assertEqual((healed.group_name, healed.category_name), ("Ore", "Material"))
        # Only the missing half is retried: the type beside it is not asked for a second time.
        self.assertEqual(self.paths(), [f"/universe/groups/{GROUP_ORES}",
                                        f"/universe/categories/{CATEGORY_MATERIAL}"])

    def test_corrupt_document_reads_as_empty_and_is_rewritten(self):
        self.write_doc("{ this is not json")
        self.assertEqual(universe.type_info(self.client, [TYPE_ORE])[TYPE_ORE].name, "Tritanium")
        self.assertIn(str(TYPE_ORE), self.doc()["types"])

    def test_half_broken_records_are_dropped_not_crashed_on(self):
        # A hand-edited file: one usable type, one record with no name, one keyed by a word.
        self.write_doc({"version": universe.CACHE_VERSION,
                        "types": {str(TYPE_ORE): {"name": "Tritanium", "group_id": GROUP_ORES,
                                                  "volume": 0.02, "packaged_volume": 0.02},
                                  str(TYPE_SHIP): {"group_id": GROUP_FRIGATES},
                                  "nonsense": {"name": "Nothing"}},
                        "groups": {}, "categories": {}})
        infos = universe.type_info(self.client, [TYPE_ORE, TYPE_SHIP])

        self.assertEqual(infos[TYPE_ORE].name, "Tritanium")      # kept as cached
        self.assertEqual(infos[TYPE_SHIP].name, "Rifter")        # refetched, not read as a crash
        self.assertNotIn("nonsense", self.doc()["types"])

    def test_document_from_a_future_version_is_discarded(self):
        universe.type_info(self.client, [TYPE_ORE])
        stale = self.doc()
        stale["version"] = universe.CACHE_VERSION + 1
        self.write_doc(stale)
        self.server.calls.clear()

        self.assertEqual(universe.type_info(self.client, [TYPE_ORE])[TYPE_ORE].name, "Tritanium")
        self.assertIn(f"/universe/types/{TYPE_ORE}", self.paths())

    def test_concurrent_runs_merge_instead_of_clobbering(self):
        """Two runs that both read an empty cache must both survive in the file.

        Each run is held with its freshly-read (empty) document until its sibling has read too, so
        both in-memory views are provably stale at publish time - exactly the interleaving a plain
        "write what I have" loses."""
        barrier = threading.Barrier(2, timeout=10)
        reads = itertools.count()
        real_read = universe.read_doc

        def read_together(cache_dir=None):
            doc = real_read(cache_dir)
            if next(reads) < 2:      # only the two opening reads wait for each other; the
                barrier.wait()       # re-read inside the publish lock must not deadlock
            return doc

        with mock.patch.object(universe, "read_doc", read_together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda i: universe.type_info(self.client, [i]),
                                        (TYPE_ORE, TYPE_SHIP)))

        self.assertEqual(results[0][TYPE_ORE].name, "Tritanium")
        self.assertEqual(results[1][TYPE_SHIP].name, "Rifter")
        doc = self.doc()
        self.assertEqual(sorted(doc["types"]), [str(TYPE_ORE), str(TYPE_SHIP)])
        self.assertEqual(sorted(doc["groups"]), [str(GROUP_ORES), str(GROUP_FRIGATES)])
        self.assertEqual(sorted(doc["categories"]), [str(CATEGORY_MATERIAL), str(CATEGORY_SHIP)])


class LocationChainTests(UniverseTestCase):
    """Nesting: which item sits in which, and what happens when the chain lies."""

    def rows(self):
        return [
            asset_row(SHIP_ITEM, TYPE_SHIP, STATION_JITA, singleton=True),
            asset_row(DRUM_ITEM, TYPE_MODULE, SHIP_ITEM, singleton=True, location_type="item",
                      flag="Cargo"),
            asset_row(MODULE_ITEM, TYPE_MODULE, DRUM_ITEM, location_type="item", flag="Cargo"),
            asset_row(FITTED_ITEM, TYPE_MODULE, SHIP_ITEM, location_type="item", flag="HiSlot0"),
        ]

    def test_chain_reports_kind_name_and_parent_for_each_level(self):
        places = self.resolve(self.rows(), token=ADA.token)

        self.assertEqual(places[STATION_JITA].kind, "station")
        self.assertEqual(places[STATION_JITA].name, self.env.names[STATION_JITA])
        self.assertIsNone(places[STATION_JITA].parent_id)

        ship, drum = places[SHIP_ITEM], places[DRUM_ITEM]
        self.assertEqual((ship.kind, ship.name, ship.parent_id), ("ship", "Nightwatch", STATION_JITA))
        self.assertEqual((drum.kind, drum.name, drum.parent_id), ("container", "Ammo Drum", SHIP_ITEM))

    def test_a_row_is_rendered_by_walking_parents_upward(self):
        places = self.resolve(self.rows(), token=ADA.token)
        row = self.rows()[2]                       # the module inside the drum inside the ship

        chain, ident, steps = [], row["location_id"], 0
        while ident is not None:
            chain.append(places[ident].name)
            ident, steps = places[ident].parent_id, steps + 1
            self.assertLess(steps, 10, "the parent walk never stopped")

        self.assertEqual(chain, ["Ammo Drum", "Nightwatch", self.env.names[STATION_JITA]])

    def test_only_ids_that_can_hold_something_are_described(self):
        places = self.resolve(self.rows(), token=ADA.token)
        # Neither module holds anything and neither is a singleton: they are rows, not places, and a
        # caller must not be able to mistake them for one.
        self.assertNotIn(MODULE_ITEM, places)
        self.assertNotIn(FITTED_ITEM, places)
        self.assertEqual(sorted(places), sorted([STATION_JITA, SHIP_ITEM, DRUM_ITEM]))

    def test_a_singleton_that_holds_nothing_is_not_called_a_container(self):
        """`is_singleton` is uniqueness, not capacity: only proof of holding counts."""
        rows = [asset_row(SHIP_ITEM, TYPE_SHIP, STATION_JITA, singleton=True),
                asset_row(DRUM_ITEM, TYPE_MODULE, SHIP_ITEM, singleton=True, location_type="item")]
        places = self.resolve(rows, token=ADA.token)

        self.assertEqual(places[DRUM_ITEM].kind, "other")
        self.assertEqual(places[SHIP_ITEM].kind, "ship")          # by its own type category

    def test_cycle_is_cut_so_the_walk_terminates(self):
        rows = [asset_row(11, TYPE_SHIP, 12, singleton=True, location_type="item"),
                asset_row(12, TYPE_SHIP, 11, singleton=True, location_type="item")]
        places = self.resolve(rows, token=ADA.token)

        for ident in (11, 12):
            self.assertIsNone(places[ident].parent_id)
            self.assertEqual(places[ident].name, "Rifter")        # still named from the catalogue

    def test_chain_longer_than_any_fitting_is_cut(self):
        depth = universe.MAX_CHAIN_HOPS + 20
        rows = [asset_row(i, TYPE_MODULE, i + 1, location_type="item") for i in range(1, depth)]
        places = self.resolve(rows, token=ADA.token)

        # Item 2 hangs under more parents than any real fitting has, so its link is cut instead of
        # followed; every described item still ends on a place.
        self.assertIsNone(places[2].parent_id)
        for ident in (2, universe.MAX_CHAIN_HOPS, depth - 1):
            steps, next_ident = 0, ident
            while next_ident is not None:
                next_ident, steps = places[next_ident].parent_id, steps + 1
                self.assertLess(steps, universe.MAX_CHAIN_HOPS + 2, "the walk never stopped")

    def test_missing_parent_degrades_to_an_item_label(self):
        rows = [asset_row(SHIP_ITEM, TYPE_SHIP, ORPHAN_PARENT, singleton=True, location_type="item")]
        places = self.resolve(rows, token=ADA.token)

        self.assertEqual(places[ORPHAN_PARENT].kind, "other")
        self.assertEqual(places[ORPHAN_PARENT].name, f"item {ORPHAN_PARENT}")
        # The item it sits in is still described, and still by the name its owner gave it.
        self.assertEqual(places[SHIP_ITEM].name, "Nightwatch")


class PlaceKindTests(UniverseTestCase):
    """Stations, solar systems, and the structures a token may or may not be allowed to see."""

    def citadel_rows(self):
        return [asset_row(SHIP_ITEM, TYPE_SHIP, CITADEL, singleton=True),
                asset_row(DRUM_ITEM, TYPE_MODULE, STATION_JITA)]

    def names_batches(self) -> list[list[int]]:
        return [call.json for call in self.server.calls_to("/universe/names")]

    def test_loose_in_space_is_a_system_named_by_its_own_id(self):
        rows = [asset_row(SHIP_ITEM, TYPE_SHIP, SYSTEM_FORGE, singleton=True,
                          location_type="solar_system", flag="SmallShipHangarAnchor0")]
        places = self.resolve(rows)

        system = places[SYSTEM_FORGE]
        self.assertEqual((system.kind, system.name), ("system", self.env.names[SYSTEM_FORGE]))
        self.assertEqual((system.system_id, system.system_name),
                         (SYSTEM_FORGE, self.env.names[SYSTEM_FORGE]))
        # A ship loose in the system inherits it, so a caller has one place to read the region from.
        self.assertEqual(places[SHIP_ITEM].system_id, SYSTEM_FORGE)

    def test_structure_refusal_degrades_and_never_poisons_the_names_batch(self):
        self.server.get(f"/universe/structures/{CITADEL}", error=(403, {"error": "forbidden"}),
                        token=ADA.token)
        places = self.resolve(self.citadel_rows(), token=ADA.token)

        self.assertEqual(len(self.server.calls_to(f"/universe/structures/{CITADEL}")), 1)
        self.assertEqual(places[CITADEL].kind, "structure")
        self.assertEqual(places[CITADEL].name, f"structure {CITADEL}")
        # The whole point of the int32 guard: one oversized id must not cost the station beside it.
        self.assertEqual(places[STATION_JITA].name, self.env.names[STATION_JITA])
        for batch in self.names_batches():
            self.assertNotIn(CITADEL, batch)
            self.assertTrue(all(i <= universe.INT32_MAX for i in batch), batch)
        with open(os.path.join(os.path.dirname(universe.doc_path()), "names.json"), encoding="utf-8") as fh:
            self.assertNotIn(str(CITADEL), json.load(fh))

    def test_readable_structure_names_itself_and_carries_its_system(self):
        self.server.get(f"/universe/structures/{CITADEL}", token=ADA.token, doc={
            "name": "Verge Keep", "owner_id": 98000001, "solar_system_id": SYSTEM_FORGE,
            "type_id": 35816})
        places = self.resolve(self.citadel_rows(), token=ADA.token)

        keep = places[CITADEL]
        self.assertEqual((keep.kind, keep.name), ("structure", "Verge Keep"))
        self.assertEqual((keep.system_id, keep.system_name), (SYSTEM_FORGE, self.env.names[SYSTEM_FORGE]))
        self.assertEqual(places[SHIP_ITEM].system_id, SYSTEM_FORGE)   # propagated down the chain

    def test_without_a_token_nothing_personal_is_asked(self):
        self.server.get(f"/universe/structures/{CITADEL}", error=(403, {"error": "forbidden"}),
                        token=ADA.token)
        places = universe.resolve_locations(self.client, self.citadel_rows(), token=None)

        self.assertEqual(self.paths_under("/universe/structures/"), [])
        self.assertEqual([p for p in self.paths() if p.endswith("/assets/names")], [])
        self.assertEqual(places[CITADEL].name, f"structure {CITADEL}")
        # The public half still works: the station is named from /universe/names.
        self.assertEqual(places[STATION_JITA].name, self.env.names[STATION_JITA])

    def test_token_without_the_structure_scope_is_not_probed(self):
        """A 403 costs error-window budget; a token that cannot win is not sent in."""
        self.server.get(f"/universe/structures/{CITADEL}", error=(403, {"error": "forbidden"}),
                        token=ADA.token)
        limited = jwt({"sub": f"CHARACTER:EVE:{ADA.character_id}",
                       "scp": ["esi-assets.read_assets.v1"]})
        places = self.resolve(self.citadel_rows(), token=limited)

        self.assertEqual(self.paths_under("/universe/structures/"), [])
        self.assertEqual(places[CITADEL].name, f"structure {CITADEL}")

    def test_an_item_is_never_probed_as_a_structure(self):
        """ESI calls the parent of these rows an item; asking /universe/structures about it is a 404."""
        self.server.get(f"/universe/structures/{CITADEL}", error=(403, {"error": "forbidden"}),
                        token=ADA.token)
        rows = [asset_row(SHIP_ITEM, TYPE_SHIP, CITADEL, singleton=True, location_type="item")]
        places = self.resolve(rows, token=ADA.token)

        self.assertEqual(self.paths_under("/universe/structures/"), [])
        self.assertEqual(places[CITADEL].name, f"item {CITADEL}")

    def test_a_row_without_a_location_type_still_gets_its_name(self):
        """A hand-made or older row can omit `location_type`; that has to cost the kind, never the name."""
        rows = [asset_row(SHIP_ITEM, TYPE_SHIP, STATION_JITA, singleton=True)]
        del rows[0]["location_type"]
        places = self.resolve(rows)

        self.assertEqual(places[STATION_JITA].kind, "other")     # genuinely unknowable from these rows
        self.assertEqual(places[STATION_JITA].name, self.env.names[STATION_JITA])


class CustomNameTests(UniverseTestCase):
    def rows(self):
        return [asset_row(SHIP_ITEM, TYPE_SHIP, STATION_JITA, singleton=True),
                asset_row(DRUM_ITEM, TYPE_MODULE, SHIP_ITEM, singleton=True, location_type="item"),
                asset_row(MODULE_ITEM, TYPE_MODULE, DRUM_ITEM, location_type="item")]

    def test_only_singletons_are_asked_about(self):
        """ESI 404s the whole chunk for one id it cannot name; singletons are the nameable ones."""
        path = self.asset_names_route(token=ADA.token)
        # The ammo drum provably holds a module but is not itself a singleton, so ESI would refuse to
        # name it - and asking about it would take the ship's real name down with the whole chunk.
        rows = [asset_row(SHIP_ITEM, TYPE_SHIP, STATION_JITA, singleton=True),
                asset_row(DRUM_ITEM, TYPE_MODULE, SHIP_ITEM, location_type="item"),
                asset_row(MODULE_ITEM, TYPE_MODULE, DRUM_ITEM, location_type="item")]
        places = self.resolve(rows, token=ADA.token)

        asked = [i for call in self.server.calls_to(path) for i in call.json]
        self.assertEqual(asked, [SHIP_ITEM])
        self.assertEqual(places[DRUM_ITEM].kind, "container")            # by the row inside it
        self.assertEqual(places[DRUM_ITEM].name, "Sensor Booster I")     # from the catalogue

    def test_refused_asset_names_leave_the_type_names(self):
        self.asset_names_route(token=ADA.token, error=(403, {"error": "no consent"}))
        places = self.resolve(self.rows(), token=ADA.token)

        self.assertEqual(places[SHIP_ITEM].name, "Rifter")         # from the type catalogue
        self.assertEqual(places[DRUM_ITEM].name, "Sensor Booster I")

    def test_esis_none_placeholder_is_not_treated_as_a_players_label(self):
        """Unnamed items come back with the literal string "None" (verified live), not null: taking
        it at face value renders every unnamed hull as `None (Rifter)`."""
        self.asset_names_route(token=ADA.token, table={SHIP_ITEM: "None", DRUM_ITEM: "  None  "})
        places = self.resolve(self.rows(), token=ADA.token)

        self.assertEqual(places[SHIP_ITEM].name, "Rifter")               # the type, not "None"
        self.assertEqual(places[DRUM_ITEM].name, "Sensor Booster I")

    def test_owner_comes_from_the_token_when_the_caller_gives_no_id(self):
        """The published contract is (client, rows, token, corporation_id); this is exactly that call."""
        token = jwt({"sub": f"CHARACTER:EVE:{ADA.character_id}", "scp": ["esi-assets.read_assets.v1"]})
        path = self.asset_names_route(token=token)
        places = universe.resolve_locations(self.client, self.rows(), token)

        self.assertEqual(len(self.server.calls_to(path)), 1)
        self.assertEqual(places[SHIP_ITEM].name, "Nightwatch")

    def test_corporation_rows_ask_the_corporation_endpoint(self):
        corp_path = self.asset_names_route(
            token=ADA.token, path=f"/corporations/{CORP_SHARED}/assets/names",
            table={SHIP_ITEM: "Corp Freight"})
        places = self.resolve(self.rows(), token=ADA.token, corporation_id=CORP_SHARED)

        self.assertEqual(len(self.server.calls_to(corp_path)), 1)
        self.assertEqual([p for p in self.paths() if f"/characters/{ADA.character_id}" in p], [])
        self.assertEqual(places[SHIP_ITEM].name, "Corp Freight")

    def test_more_than_a_thousand_ids_are_chunked(self):
        first_id = 900_000_100
        table = {first_id + i: f"Named {i}" for i in range(1500)}
        rows = [asset_row(ident, TYPE_MODULE, STATION_JITA, singleton=True) for ident in table]
        path = self.asset_names_route(token=ADA.token, table=table)

        places = self.resolve(rows, token=ADA.token)

        self.assertEqual([len(call.json) for call in self.server.calls_to(path)], [1000, 500])
        self.assertEqual(places[first_id].name, "Named 0")
        self.assertEqual(places[first_id + 1499].name, "Named 1499")

    def test_no_rows_describe_nothing(self):
        self.assertEqual(universe.resolve_locations(self.client, [], token=ADA.token), {})
        self.assertEqual(self.paths(), [])


class WarmRunTests(UniverseTestCase):
    def test_a_second_run_asks_only_for_what_is_personal(self):
        """The catalogue and the station names come off disk; the personal half is re-read.

        Custom names and token-visible structures change without warning - a rename, a repack - and
        the asset rows were fetched live for this run anyway, so this is the deliberate exception to
        the catalogue's "warm means silent" rule."""
        rows = [asset_row(SHIP_ITEM, TYPE_SHIP, STATION_JITA, singleton=True),
                asset_row(MODULE_ITEM, TYPE_MODULE, SHIP_ITEM, location_type="item")]
        path = self.asset_names_route(token=ADA.token)

        self.resolve(rows, token=ADA.token)
        self.server.calls.clear()
        places = self.resolve(rows, token=ADA.token)

        self.assertEqual(sorted({call.path for call in self.server.calls}), [path])
        self.assertEqual(places[STATION_JITA].name, self.env.names[STATION_JITA])
        self.assertEqual((places[SHIP_ITEM].kind, places[SHIP_ITEM].name), ("ship", "Nightwatch"))


if __name__ == "__main__":
    unittest.main()
