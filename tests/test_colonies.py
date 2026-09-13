"""`colonies` end to end: the colony list, one colony's pins, and what each output mode owes whom.

Runs against tests/fake_esi.py's in-process ESI with a synthetic planetary-industry document seeded into
`$XDG_DATA_HOME`. The two colony documents were written from ESI's own published OpenAPI description of
the routes (fetched 2026-09-13), including the parts that are easy to get wrong: `expiry_time` is absent
rather than null-ish when nothing is programmed, `heads` arrives as a list of positions on one pin and a
plain count on another, and a facility's schematic lives in `factory_details` or at the top level
depending on which version of the endpoint built the row.

What this file protects is the seams between those facts and the report: that a summary run never pays
for a layout it will not print; that one colony's refusal costs exactly that colony its pins and nothing
else - not the other colonies, not the exit code; that a name taken from ESI because the local snapshot
never heard of the schematic is *labelled* as such, since "no outputs" and "outputs nobody published"
look identical otherwise; that a missing snapshot degrades instead of refusing (every extractor column
comes from ESI, so a ~100 MB download must not gate "did my extractors run out"); and that `--csv` says
out loud when it switches from one row per colony to one row per extractor.

The stale marker is deliberately asserted both ways: firing on a nine-day-old colony and staying quiet on
a fresh one, because a marker that always fires teaches nobody anything.
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

from eve_skills import alphadata
from tests.fake_esi import (
    ADA, COLONY_PLANET_BARREN, COLONY_PLANET_ICE, COLONY_PLANET_STORM, COLONY_SYSTEM_A,
    COLONY_SYSTEM_B, PI_EXTRACTOR_HEAVY, PI_EXTRACTOR_LIGHT, PI_FACILITY_PROCESSOR, PI_PIN_COMMAND,
    PI_PIN_ECU, PI_PRODUCT_HEAVY, PI_PRODUCT_WATER, SCHEMATIC_KNOWN, SCHEMATIC_UNKNOWN, VELA,
    FakeEsiEnv, colony_row, extractor_pin, facility_pin, iso, plain_pin,
)

# ---------------------------------------------------------------------------
# the synthetic colony world
# ---------------------------------------------------------------------------
# The snapshot is deliberately small: eight planet-type names, two commodities, one structure pair and
# exactly one schematic. `SCHEMATIC_UNKNOWN` therefore has to fall back to ESI by construction rather
# than by accident, and a type the snapshot does not name (the light extractor) shows what happens when
# only /universe/names can help.
DOCUMENT = {
    "source": "synthetic", "build": 2500001, "fetched": iso(-86400),
    "planet_types": {"11": "Temperate", "12": "Ice", "13": "Gas", "2014": "Oceanic",
                     "2015": "Lava", "2016": "Barren", "2017": "Storm", "2063": "Plasma"},
    "resources": {},
    "commodities": {str(PI_PRODUCT_HEAVY): {"name": "Heavy Water"},
                    str(PI_PRODUCT_WATER): {"name": "Water"}},
    "schematics": {str(SCHEMATIC_KNOWN): {"name": "Heavy Water Processing", "cycle": 1800,
                                          "in": {str(PI_PRODUCT_WATER): 20},
                                          "out": {str(PI_PRODUCT_HEAVY): 5}}},
    "structures": {str(PI_EXTRACTOR_HEAVY): {"name": "Heavy Water Extractor Mine"},
                   str(PI_FACILITY_PROCESSOR): {"name": "Basic Processor Factory"}},
    "command_centers": {}, "tax_factors": {},
}


def ice_pins() -> list[dict]:
    """Ada's Ice colony, stamped relative to the moment a test asks for it.

    Built per test rather than at import on purpose: every expiry below is an offset from `now`, and a
    fixture frozen at import would let the assertions about "expired 2h ago" drift out of true if the
    suite ever paused between collecting and running them."""
    return [
        extractor_pin(1012799140964, PI_EXTRACTOR_HEAVY, PI_PRODUCT_HEAVY, qty=15, cycle=3600,
                      heads=[11, 12, 13], expiry=iso(-7200)),
        extractor_pin(1012799140965, PI_EXTRACTOR_LIGHT, PI_PRODUCT_WATER, qty=8, cycle=1800,
                      heads=1, expiry=iso(3 * 86400)),
        extractor_pin(1012799140966, PI_EXTRACTOR_HEAVY, None, qty=None, cycle=None, heads=None),
        facility_pin(1012799140967, PI_FACILITY_PROCESSOR, SCHEMATIC_KNOWN),
        plain_pin(1012799140968, PI_PIN_COMMAND),
        plain_pin(1012799140969, PI_PIN_ECU),
        plain_pin(1012799140970, PI_PIN_ECU),
    ]


def storm_pins() -> list[dict]:
    """An abandoned colony: one command centre and nothing running."""
    return [plain_pin(2012799140961, PI_PIN_COMMAND)]


def colonies_fixture() -> list[tuple[dict, list[dict]]]:
    """Ada's two colonies - the Ice one an hour old, the Storm one nine days past the stale threshold."""
    return [(colony_row(COLONY_PLANET_ICE, "ice", pins=7, ccu=4), ice_pins()),
            (colony_row(COLONY_PLANET_STORM, "storm", pins=1, ccu=2, age=-9 * 86400), storm_pins())]


def flat(text: str) -> str:
    """A table row with its column padding collapsed, so an assertion can name the values a reader sees
    without also pinning `render.table`'s current column widths."""
    return " ".join(text.split())


class ColoniesTestCase(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_core()
        self.write_document(DOCUMENT)
        self.addCleanup(self.env.stop)

    # -- seeding ---------------------------------------------------------------
    def write_document(self, document: dict | None) -> None:
        """Install the synthetic snapshot - or, with None, take the installed one away again, which is how
        a test says "this machine has never run `eve-skills update-data`"."""
        path = os.path.join(self.env.data_home, "eve-skills", "planet_industry.json")
        if document is None:
            if os.path.exists(path):
                os.remove(path)
            return
        self.env._write_json(path, document)

    def seed(self, planets=None, schematic=None):
        self.env.install_colonies(ADA, colonies_fixture() if planets is None else planets)
        if schematic is not None:
            self.env.install_schematic(*schematic)
        return self.env.colony_book(ADA)

    def layout_route(self, planet_id: int) -> str:
        return f"/characters/{ADA.character_id}/planets/{planet_id}"

    # -- running ---------------------------------------------------------------

    def run_ok(self, argv: list[str]) -> str:
        code, out, err = self.env.run(argv)
        self.assertEqual(code, 0, f"{argv} refused: {err}")
        return out

    def doc(self, argv: list[str]) -> dict:
        parsed = json.loads(self.run_ok([*argv, "--json"]))
        self.assertIsInstance(parsed, dict)
        return parsed

    def csv_rows(self, argv: list[str]) -> tuple[list[str], list[dict]]:
        """(header, rows as dicts), so a test names columns instead of counting commas."""
        out = self.run_ok([*argv, "--csv"])
        rows = list(csv.reader(io.StringIO(out)))
        header = rows[0]
        return header, [dict(zip(header, row)) for row in rows[1:]]

    def colony_doc(self, document: dict, planet_id: int) -> dict:
        matches = [row for character in document["characters"] for row in character["colonies"]
                   if row["planet_id"] == planet_id]
        self.assertEqual(len(matches), 1, f"want exactly one row for {planet_id}: {document}")
        return matches[0]

    # -- the summary -----------------------------------------------------------

    def test_the_table_lists_one_row_per_colony_with_es_is_own_facts(self):
        """Planet types are words, systems are names, and the stamp is an age rather than a raw ISO
        string: three places the report turns ESI's ids into something readable without asking anyone."""
        self.seed()
        out = self.run_ok(["colonies"])
        rows = [flat(line) for line in out.splitlines() if str(COLONY_PLANET_ICE) in line
                or str(COLONY_PLANET_STORM) in line]
        self.assertEqual(2, len(rows), out)
        self.assertTrue(rows[0].startswith("Ada Vane Ditalren 50008625 Ice 4 7"), rows[0])
        self.assertIn("ago", rows[0])
        self.assertNotIn("*", rows[0], "a colony updated an hour ago must not carry the stale marker")
        self.assertTrue(rows[1].startswith("Ada Vane Ditalren 50008626 Storm 2 1"), rows[1])
        self.assertIn("9d 00h ago *", rows[1])
        self.assertIn("2 colonies across 1 character", out)

    def test_the_summary_never_asks_for_a_layout_it_would_not_print(self):
        """The list endpoint already answers "do these colonies still exist" for one request per
        character. Fetching every pin document anyway would triple the calls for a table that has no
        column any of them belongs to."""
        self.seed()
        self.run_ok(["colonies"])
        self.assertEqual([], self.env.server.calls_to(self.layout_route(COLONY_PLANET_ICE)))
        self.run_ok(["colonies", "--detail"])
        self.assertEqual(1, len(self.env.server.calls_to(self.layout_route(COLONY_PLANET_ICE))))

    def test_a_stale_colony_is_marked_and_the_reason_prints_with_it(self):
        """The star means nothing on its own: ESI only recalculates a colony when it is opened in the
        client, so the footnote quoting that rule is what turns a marker into an explanation."""
        self.seed()
        out = self.run_ok(["colonies"])
        self.assertIn("*", out)
        self.assertIn("opened in the game client", out)
        self.assertIn("7 days", out)

    def test_a_fresh_colony_prints_no_stale_footnote(self):
        """A marker that always fires is noise, and the footnote with it. Same command, same fixture
        minus the nine-day-old colony: neither may appear."""
        self.seed(planets=[(colony_row(COLONY_PLANET_ICE, "ice", pins=3), ice_pins()[:3])])
        out = self.run_ok(["colonies"])
        self.assertNotIn("*", out)
        self.assertNotIn("game client", out)

    def test_a_character_without_colonies_is_answered_with_none_rather_than_silence(self):
        """The first question of the whole feature is "do my old colonies still exist". A character with
        no colonies and no line would read as a bug; with a hint it reads as an answer."""
        self.seed(planets=[])
        out = self.run_ok(["colonies"])
        self.assertIn("Ada Vane: no colonies", out)
        self.assertEqual([], self.doc(["colonies"])["characters"][0]["colonies"])

    def test_char_restricts_the_report_to_one_stored_character(self):
        self.seed()
        by_name = self.run_ok(["colonies", "--char", "Ada Vane"])
        self.assertIn(str(COLONY_PLANET_ICE), by_name)
        self.assertNotIn("Vela Krinn: no planets consent", by_name)
        by_id = self.run_ok(["colonies", "--char", str(VELA.character_id)])
        self.assertIn("Vela Krinn: no planets consent", by_id)
        self.assertNotIn(str(COLONY_PLANET_ICE), by_id)

    # -- consent ---------------------------------------------------------------

    def test_missing_consent_is_a_hint_that_costs_the_other_characters_nothing(self):
        """A refresh token keeps the scopes it was minted with, so every existing character fails this
        endpoint until its owner re-logs in. That is a hint, not an error: exit 0, and the characters who
        did consent are still reported."""
        self.seed()
        out = self.run_ok(["colonies"])
        self.assertIn("Vela Krinn: no planets consent", out)
        self.assertIn("eve-skills login --scopes planets", out)
        self.assertIn(str(COLONY_PLANET_ICE), out)
        self.assertEqual([], self.env.server.calls_to(f"/characters/{VELA.character_id}/planets"))

    # -- the detail view -------------------------------------------------------

    def test_detail_lists_every_extractor_with_the_timer_esi_gave_it(self):
        """The run-out extractor sorts first because finding it is the point of the view, an unprogrammed
        one last because it needs nothing, and both `heads` shapes have to read as a count."""
        self.seed()
        out = flat(self.run_ok(["colonies", "--detail"]))
        ran_out = "Heavy Water Extractor Mine Heavy Water 15 1h 00m 3"
        still_running = "Light Water Extractor Mine Water 8 30m 1"
        unprogrammed = "Heavy Water Extractor Mine - - - - -"
        for cell in (ran_out, still_running, unprogrammed):
            self.assertIn(cell, out)
        self.assertLess(out.index(ran_out), out.index(still_running))
        self.assertLess(out.index(still_running), out.index(unprogrammed))
        self.assertIn("(expired 2h 00m ago)", out)
        self.assertIn("(in 2d 23h)", out)

    def test_a_colony_with_nothing_running_says_so_instead_of_printing_empty_tables(self):
        self.seed()
        out = self.run_ok(["colonies", "--detail"])
        self.assertIn("(no extractors on this colony)", out)
        self.assertIn("(no facility is running a schematic)", out)
        self.assertIn("also on the layout: Command Center x1", out)

    def test_the_detail_block_counts_the_pins_it_does_not_list(self):
        """Two ECUs and one command centre are not worth six rows, but they are worth knowing about - so
        they are counted, most numerous first."""
        self.seed()
        out = self.run_ok(["colonies", "--detail"])
        self.assertIn("also on the layout: Extractor Control Unit x2, Command Center x1", out)

    def test_a_recipe_the_snapshot_knows_costs_no_request_and_names_its_output(self):
        """The shipped document has every recipe ESI ships, so the common path must not spend a request -
        and the output cell is named from the snapshot too, not left as a bare type id."""
        self.seed()
        out = self.run_ok(["colonies", "--detail"])
        self.assertIn("Basic Processor Factory Heavy Water Processing 30m Heavy Water x5", flat(out))
        self.assertEqual([], self.env.server.calls_to(f"/universe/schematics/{SCHEMATIC_KNOWN}"))

    def test_a_recipe_the_snapshot_never_heard_of_is_named_by_esi_and_says_so(self):
        """`/universe/schematics` publishes a name and a cycle and no recipe. Without the notice, an empty
        `makes` cell would read as "this facility produces nothing" - a different claim entirely."""
        self.seed(planets=[(colony_row(COLONY_PLANET_ICE, "ice", pins=2), [
            facility_pin(11, PI_FACILITY_PROCESSOR, SCHEMATIC_UNKNOWN)])],
            schematic=(SCHEMATIC_UNKNOWN, "Crystalloid Processing", 1800))
        out = self.run_ok(["colonies", "--detail"])
        self.assertIn("Crystalloid Processing", out)
        self.assertEqual(1, len(self.env.server.calls_to(f"/universe/schematics/{SCHEMATIC_UNKNOWN}")))
        notice = [line for line in out.splitlines() if "not in the local SDE snapshot" in line]
        self.assertEqual(1, len(notice), out)
        self.assertIn(str(SCHEMATIC_UNKNOWN), notice[0])
        self.assertIn("eve-skills update-data", notice[0])

    def test_no_local_snapshot_still_answers_everything_esi_can_answer(self):
        """A fresh install has no planetary snapshot, and every extractor column comes from ESI. Refusing
        the whole view there would gate "did my extractors run out" behind a ~100 MB download, so names
        come from `/universe/names`, the schematic from its own endpoint, and both are labelled."""
        self.seed(planets=[(colony_row(COLONY_PLANET_ICE, "ice", pins=2), [
            extractor_pin(21, PI_EXTRACTOR_HEAVY, PI_PRODUCT_HEAVY, expiry=iso(-60)),
            facility_pin(22, PI_FACILITY_PROCESSOR, SCHEMATIC_UNKNOWN)])],
            schematic=(SCHEMATIC_UNKNOWN, "Crystalloid Processing", 1800))
        self.write_document(None)
        with mock.patch.object(alphadata, "PACKAGE_DATA_DIR", Path(tempfile.mkdtemp())):
            out = self.run_ok(["colonies", "--detail"])
        self.assertIn("Heavy Water Extractor Mine Heavy Water 15 1h 00m - ", flat(out))
        self.assertIn("expired", out)
        self.assertIn("Crystalloid Processing", out)
        self.assertEqual(1, len(self.env.server.calls_to(f"/universe/schematics/{SCHEMATIC_UNKNOWN}")))
        self.assertIn("no local planetary industry data", out)
        self.assertIn("eve-skills update-data", out)
        self.assertIn("build unknown", out, "the notice must not claim a build it cannot see")

    def test_one_unreadable_colony_keeps_its_row_and_warns_instead_of_failing_the_run(self):
        """The list is what answers "do my colonies still exist", so one planet's pins failing to read may
        cost that colony its extractors and nothing more: the row stays, the other colony renders, exit 0.
        """
        state = self.seed()
        state.layouts[COLONY_PLANET_STORM].error = (503, {"error": "ESI is having a day"})
        code, out, err = self.env.run(["colonies", "--detail"])
        self.assertEqual(0, code, err)
        self.assertIn(str(COLONY_PLANET_STORM), out)
        self.assertIn("Heavy Water Extractor Mine", out)
        self.assertIn(f"the layout of planet {COLONY_PLANET_STORM} could not be read", err)
        self.assertIn("ESI is having a day", err)
        self.assertIn("this colony's pins could not be read", out)

    def test_json_distinguishes_a_layout_never_asked_for_from_an_empty_one(self):
        """Both have no extractors to show, and conflating them would report "this colony has none" from a
        run that never looked."""
        self.seed()
        summary = self.doc(["colonies"])
        ice = self.colony_doc(summary, COLONY_PLANET_ICE)
        self.assertFalse(ice["layout_fetched"])
        self.assertNotIn("extractors", ice)
        detailed = self.doc(["colonies", "--detail"])
        storm = self.colony_doc(detailed, COLONY_PLANET_STORM)
        self.assertTrue(storm["layout_fetched"])
        self.assertEqual([], storm["extractors"])
        self.assertEqual(7, self.colony_doc(detailed, COLONY_PLANET_ICE)["pins_read"])

    def test_json_gives_an_expiry_both_ways_and_never_calls_an_unprogrammed_pin_expired(self):
        """`seconds_to_expiry` is negative once an extraction ran out - the sort key a spreadsheet wants -
        and absent for a pin with nothing programmed, which is not the same state as expired."""
        self.seed()
        extractors = {row["pin_id"]: row
                      for row in self.colony_doc(self.doc(["colonies", "--detail"]),
                                                 COLONY_PLANET_ICE)["extractors"]}
        self.assertLess(extractors[1012799140964]["seconds_to_expiry"], 0)
        self.assertTrue(extractors[1012799140964]["expired"])
        self.assertGreater(extractors[1012799140965]["seconds_to_expiry"], 2 * 86400)
        self.assertFalse(extractors[1012799140965]["expired"])
        self.assertIsNone(extractors[1012799140966]["seconds_to_expiry"])
        self.assertFalse(extractors[1012799140966]["expired"])
        self.assertIsNone(extractors[1012799140966]["product_name"])

    def test_json_carries_hints_warnings_and_notices_as_fields(self):
        """Prose would break a parser, so all three travel as data - including the schematic notice that
        the table view prints inline."""
        self.seed(planets=[(colony_row(COLONY_PLANET_ICE, "ice", pins=1), [
            facility_pin(11, PI_FACILITY_PROCESSOR, SCHEMATIC_UNKNOWN)])],
            schematic=(SCHEMATIC_UNKNOWN, "Crystalloid Processing", 1800))
        document = self.doc(["colonies", "--detail"])
        self.assertEqual([ADA.character_id], [row["character_id"] for row in document["characters"]],
                         "a character without consent is a hint, never a character entry")
        self.assertTrue(any("planets consent" in line for line in document["hints"]), document["hints"])
        self.assertEqual([], document["warnings"])
        self.assertEqual(1, len(document["notices"]), document["notices"])
        self.assertEqual(7, document["stale_after_days"])
        self.assertTrue(document["detail"])

    # -- csv -------------------------------------------------------------------

    def test_csv_defaults_to_one_row_per_colony(self):
        self.seed()
        header, rows = self.csv_rows(["colonies"])
        self.assertEqual(["character_id", "character_name", "solar_system_id", "system_name",
                          "planet_id", "planet_type", "planet_type_name", "owner_id", "upgrade_level",
                          "num_pins", "last_update", "age_seconds", "stale"], header)
        self.assertEqual(2, len(rows))
        by_planet = {int(row["planet_id"]): row for row in rows}
        self.assertEqual("Ditalren", by_planet[COLONY_PLANET_ICE]["system_name"])
        self.assertEqual("ice", by_planet[COLONY_PLANET_ICE]["planet_type"], "ESI's enum, verbatim")
        self.assertEqual("Ice", by_planet[COLONY_PLANET_ICE]["planet_type_name"])
        self.assertEqual("0", by_planet[COLONY_PLANET_ICE]["stale"])
        self.assertEqual("1", by_planet[COLONY_PLANET_STORM]["stale"])

    def test_csv_with_detail_switches_to_one_row_per_extractor(self):
        """Granularity follows the flag, the way `inventory --items` does it: colony columns repeat so
        every row still says where that extractor stands."""
        self.seed()
        header, rows = self.csv_rows(["colonies", "--detail"])
        self.assertEqual(header[:13], ["character_id", "character_name", "solar_system_id",
                                       "system_name", "planet_id", "planet_type", "planet_type_name",
                                       "owner_id", "upgrade_level", "num_pins", "last_update",
                                       "age_seconds", "stale"])
        self.assertEqual(["pin_id", "extractor_type_id", "product_type_id", "product_name",
                          "qty_per_cycle", "cycle_seconds", "heads", "expiry_time", "expired",
                          "seconds_to_expiry"], header[13:])
        ice = [row for row in rows if int(row["planet_id"]) == COLONY_PLANET_ICE]
        self.assertEqual(3, len(ice))
        self.assertEqual("15", ice[0]["qty_per_cycle"])
        self.assertEqual("Heavy Water", ice[0]["product_name"])
        self.assertEqual("1", ice[0]["expired"])

    def test_a_colony_without_extractors_keeps_its_csv_row(self):
        """"No extractors" is still an answer about a colony that exists. Dropping the row would let a
        reader conclude the colony was not there at all."""
        self.seed()
        _header, rows = self.csv_rows(["colonies", "--detail"])
        storm = [row for row in rows if int(row["planet_id"]) == COLONY_PLANET_STORM]
        self.assertEqual(1, len(storm))
        self.assertEqual("", storm[0]["pin_id"])
        self.assertEqual("", storm[0]["expiry_time"])
        self.assertEqual("Ada Vane", storm[0]["character_name"])
        self.assertEqual("2", storm[0]["upgrade_level"])

    def test_csv_writes_only_rows_to_stdout(self):
        """The consent hint and the schematic notice both matter to whoever opens the spreadsheet, and
        neither may land in the pipe."""
        self.seed(planets=[(colony_row(COLONY_PLANET_ICE, "ice", pins=1), [
            facility_pin(11, PI_FACILITY_PROCESSOR, SCHEMATIC_UNKNOWN)])],
            schematic=(SCHEMATIC_UNKNOWN, "Crystalloid Processing", 1800))
        code, out, err = self.env.run(["colonies", "--detail", "--csv"])
        self.assertEqual(0, code, err)
        self.assertTrue(out.startswith("character_id,"), out[:80])
        self.assertNotIn("consent", out)
        self.assertIn("no planets consent", err)
        self.assertIn("not in the local SDE snapshot", err)


if __name__ == "__main__":
    unittest.main()
