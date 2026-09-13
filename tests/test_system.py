"""`system`: the security-class rule, the planet census, and what a route comparison has to say.

Runs against tests/fake_esi.py's in-process ESI with a small census seeded into
`$XDG_DATA_HOME/eve-skills/system_planets.json`. Four of the systems are real ones, because the
boundary case this command exists for only exists in real data: measured against live ESI on
2026-09-13, Enderailen sits at 0.4487847685813904 and Kulelen at 0.4753689467906952 - twenty-six
ten-thousandths apart - and EVE shows them as 0.4 (lowsec) and 0.5 (highsec). Their security figures
below are ESI's own, and their census rows are the shipped document's counts for them, so a change
here means New Eden moved rather than that this file drifted. Everything else - the route paths, the
`/universe/constellations` records, the invented system that carries a Shattered planet - is fixture.

What this file protects: classification on the true value while the displayed figure follows EVE's
round-away-from-zero rule (which Python's `round` does not), a run straddling a class line called
out instead of left to the reader, planet types read from the census including the kinds nobody can
colonise, a system with no census row reported as an answer rather than as zero planets, `shortest`
and `secure` both fetched so a jump count is quoted against its sibling, one route or one planet
list failing costing a cell and a note instead of the report, and no request at all leaving before
the local census has been found.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eve_skills import alphadata, cmd_system
from tests.fake_esi import MARKET_IDS, SYSTEM_FORGE, FakeEsiEnv, http_error, iso

# ---------------------------------------------------------------------------
# The universe. Rairomon is the control: 0.6294 is nowhere near a class line, so it is what every
# "this report says nothing about rounding" assertion compares against, and Zarzakh is the system
# ESI gives no `planets` key for at all (measured live 2026-09-13) as well as the one the SDE has no
# planets in - which is why it reads `-` rather than `0`.

ENDERAELEN, RAIROMON, KULELEN = 30002769, 30002772, 30002771
ZARZAKH = 30100000
SHATTERED, SHATTERED_NAME = 39000001, "Glass Reach"     # invented: the row with an uncolonisable type

SECURITY = {ENDERAELEN: 0.4487847685813904, RAIROMON: 0.6293596029281616,
            KULELEN: 0.4753689467906952, ZARZAKH: -1.0, SHATTERED: 0.0499, SYSTEM_FORGE: 0.9}
NAMES = {ENDERAELEN: "Enderailen", RAIROMON: "Rairomon", KULELEN: "Kulelen", ZARZAKH: "Zarzakh",
         SHATTERED: SHATTERED_NAME, SYSTEM_FORGE: "Jita"}

KANTANEN, CITADEL = 20000406, 10000033          # the three Caldari-Gallente systems live here
WORMHOLE_SPAN, DEEP_SPAN = 39000101, 39000201   # invented spans, for the two invented systems
THE_CITADEL, FORGE = 20000020, 10000002         # Jita's real constellation and region
CONSTELLATION_OF = {ENDERAELEN: KANTANEN, RAIROMON: KANTANEN, KULELEN: KANTANEN,
                    ZARZAKH: WORMHOLE_SPAN, SHATTERED: DEEP_SPAN, SYSTEM_FORGE: THE_CITADEL}
CONSTELLATIONS = {KANTANEN: ("Kantanen", CITADEL), WORMHOLE_SPAN: ("Duzna Kah", CITADEL),
                  DEEP_SPAN: ("Glass Span", CITADEL), THE_CITADEL: ("The Citadel", FORGE)}

# The shipped document's rows for the three real systems, plus the invented ones.
CENSUS = {
    "source": "eve-online-static-data-test", "build": 3494416, "fetched": "2026-09-04",
    "planet_types": {"11": "Temperate", "12": "Ice", "13": "Gas", "2014": "Oceanic",
                     "2015": "Lava", "2016": "Barren", "2017": "Storm", "2063": "Plasma",
                     "30889": "Shattered"},
    "systems": {
        str(RAIROMON): {"11": 2, "2014": 1, "2015": 1, "2016": 3, "2017": 2, "2063": 2},
        str(ENDERAELEN): {"12": 1, "13": 5, "2015": 1, "2017": 2},
        str(KULELEN): {"13": 2, "2015": 2, "2016": 1, "2063": 2},
        str(SHATTERED): {"30889": 4, "2016": 1},
    },
}


def system_document(system_id: int, planets: int | None = ...) -> dict:
    """`/universe/systems/{id}` as live ESI answers it. A system with no census row gets no `planets`
    key at all, which is what ESI itself does for a planetless system rather than answering `[]`;
    passing `planets=` overrides that, which is how the two sources are made to disagree."""
    counts = CENSUS["systems"].get(str(system_id), {})
    count = sum(counts.values()) if planets is ... else planets
    document = {"system_id": system_id, "name": NAMES[system_id], "security_status": SECURITY[system_id],
                "constellation_id": CONSTELLATION_OF[system_id], "star_id": 10 * system_id}
    if count:
        document["planets"] = [90_000_000 + system_id * 100 + step for step in range(count)]
    return document


# ---------------------------------------------------------------------------
class SecurityRuleTests(unittest.TestCase):
    """The figure EVE shows and the class it means, tested on the values that motivated them."""

    def test_the_two_systems_twenty_six_thousandths_apart_are_not_one_class(self):
        """Both readings pinned at full precision, so a refactor to `round(security, 1)` - which is
        what every other tool in this family got wrong - fails on the pair that proves it wrong."""
        self.assertEqual((0.4, "low"), (cmd_system.display_security(SECURITY[ENDERAELEN]),
                                        cmd_system.security_class(SECURITY[ENDERAELEN])))
        self.assertEqual((0.5, "high"), (cmd_system.display_security(SECURITY[KULELEN]),
                                         cmd_system.security_class(SECURITY[KULELEN])))

    def test_the_highsec_line_is_0_45_on_the_true_value(self):
        """`x >= 0.45` is highsec and anything above zero below that is lowsec, whatever the client
        shows: 0.4499 still displays 0.4 and stays lowsec."""
        for value in (0.4487847685813904, 0.4499):
            self.assertEqual("low", cmd_system.security_class(value), f"{value} read as highsec")
        self.assertEqual("high", cmd_system.security_class(0.45))

    def test_halves_round_away_from_zero_the_way_the_client_does(self):
        """Python's `round` rounds halves to even, so it would print 0.45 as 0.4 where the client
        prints 0.5; CCP's own snippet uses Java/Kotlin `Math.round`, whose worked example is -0.45 ->
        -0.4. Both sides of zero are pinned because a floor and a truncation differ exactly here."""
        self.assertEqual(0.5, cmd_system.display_security(0.45))
        self.assertEqual(-0.4, cmd_system.display_security(-0.45))
        self.assertEqual(1.0, cmd_system.display_security(1.0))
        self.assertNotEqual(cmd_system.display_security(0.45), round(0.45 * 10) / 10)

    def test_a_positive_status_below_0_05_displays_0_1_and_is_still_lowsec(self):
        """CCP's stated exception, and the reason it is not cosmetic: a system showing 0.0 reads as
        shoot-on-sight space, so 0.0499 has to display 0.1 while classifying as lowsec."""
        for value in (0.0499, 0.0001):
            self.assertEqual(0.1, cmd_system.display_security(value))
            self.assertEqual("low", cmd_system.security_class(value))

    def test_zero_displays_as_zero_and_is_nullsec_on_both_sides(self):
        """`-0.0` is a float a rounding rule can produce and `f"{-0.0:.1f}"` prints "-0.0" on every
        terminal this tool runs on, so the sign is dropped deliberately."""
        for value in (0.0, -0.0):
            self.assertEqual(0.0, cmd_system.display_security(value))
            self.assertEqual("0.0", f"{cmd_system.display_security(value):.1f}")
            self.assertEqual("null", cmd_system.security_class(value))
        self.assertEqual("null", cmd_system.security_class(-0.45))


# ---------------------------------------------------------------------------
class PlanetCensusTransformTests(unittest.TestCase):
    """`alphadata._transform_system_planets`: every planet counted, every type named."""

    def test_every_type_in_the_data_is_counted_including_the_uncolonisable_ones(self):
        """`planet_industry.json` names the eight colonisable types only; a census that copied that
        list would silently drop a Shattered planet from the system being weighed, so the count and
        the name both come out of the data."""
        planets = [{"_key": 1, "solarSystemID": RAIROMON, "typeID": 13},
                   {"_key": 2, "solarSystemID": RAIROMON, "typeID": 30889},
                   {"_key": 3, "solarSystemID": RAIROMON, "typeID": 13},
                   {"_key": 4, "solarSystemID": ENDERAELEN, "typeID": 30889}]
        types = [{"_key": 13, "name": {"en": "Planet (Gas)"}},
                 {"_key": 30889, "name": {"en": "Planet (Shattered)"}}]
        document = alphadata._transform_system_planets(planets, types)
        self.assertEqual({"13": "Gas", "30889": "Shattered"}, document["planet_types"])
        self.assertEqual({str(RAIROMON): {"13": 2, "30889": 1}, str(ENDERAELEN): {"30889": 1}},
                         document["systems"])

    def test_keys_are_ordered_numerically_so_a_rebuild_differs_by_nothing(self):
        """Rebuilding an unchanged SDE has to produce the same bytes, and string ids only sort the way
        people read them when the sort is done on the integer."""
        planets = [{"_key": 1, "solarSystemID": 3002, "typeID": 30889},
                   {"_key": 2, "solarSystemID": 3000, "typeID": 2014},
                   {"_key": 3, "solarSystemID": 3001, "typeID": 13},
                   {"_key": 4, "solarSystemID": 3000, "typeID": 13}]
        document = alphadata._transform_system_planets(planets, [])
        self.assertEqual(["3000", "3001", "3002"], list(document["systems"]))
        self.assertEqual({"3000": {"13": 1, "2014": 1}, "3001": {"13": 1}, "3002": {"30889": 1}},
                         document["systems"])
        self.assertEqual(["13", "2014", "30889"], list(document["planet_types"]))

    def test_a_counted_type_with_no_name_row_keeps_its_id_in_the_label(self):
        """The SDE's planet types are unpublished marker rows, so a counted id can reach the census
        with no name in `types.jsonl`; a placeholder carrying the id beats dropping the planet."""
        document = alphadata._transform_system_planets(
            [{"_key": 1, "solarSystemID": RAIROMON, "typeID": 99123}], [])
        self.assertEqual("unnamed planet type 99123", document["planet_types"]["99123"])

    def test_a_row_without_a_system_or_a_type_is_not_counted(self):
        """`update()` streams one row at a time, so a malformed line has to be skipped without
        inventing a census entry for it and without losing the well-formed rows around it."""
        document = alphadata._transform_system_planets(
            [{"_key": 1, "typeID": 13}, {"_key": 2, "solarSystemID": RAIROMON},
             {"_key": 3, "solarSystemID": RAIROMON, "typeID": 13}], [])
        self.assertEqual({str(RAIROMON): {"13": 1}}, document["systems"])


# ---------------------------------------------------------------------------
class SystemCommandTestCase(unittest.TestCase):
    """The four systems served from `/universe/systems`, one constellation document each, and a
    census in `$XDG_DATA_HOME` so the planet column and `--route` both have something to read."""

    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.addCleanup(self.env.stop)
        self.env.install_market()          # POST /universe/ids, driven by MARKET_IDS
        self.env.server.post("/universe/names", handler=self.env._names_handler)
        self.env.names.update({CITADEL: "The Citadel", FORGE: "The Forge", **NAMES})
        self.ids = mock.patch.dict(MARKET_IDS, {name: {"systems": [ident]}
                                                for ident, name in NAMES.items()})
        self.ids.start()
        self.addCleanup(self.ids.stop)
        for system_id in SECURITY:
            self.env.server.get(f"/universe/systems/{system_id}",
                                handler=lambda call, ident=system_id: system_document(ident))
        for constellation_id, (name, region_id) in CONSTELLATIONS.items():
            self.env.server.get(f"/universe/constellations/{constellation_id}",
                                doc={"id": constellation_id, "name": name, "region_id": region_id})
        self.write_census(CENSUS)

    # -- fixtures the scenarios rewrite ------------------------------------

    def write_census(self, document: dict) -> None:
        self.env._write_json(os.path.join(self.env.data_home, "eve-skills", "system_planets.json"),
                             document)

    def census_row(self, system_id: int, counts: dict | None) -> None:
        """Rewrite one system's row and keep the rest of the document as shipped, so a test that
        changes a planet mix does not also change what every other system in the fixture says."""
        systems = dict(CENSUS["systems"])
        if counts is None:
            systems.pop(str(system_id), None)
        else:
            systems[str(system_id)] = {str(type_id): count for type_id, count in counts.items()}
        self.write_census({**CENSUS, "systems": systems})
        self.env.server.get(f"/universe/systems/{system_id}",
                            handler=lambda call, ident=system_id: system_document(ident))

    def route(self, origin: int, destination: int, **paths_by_flag):
        """One `/route` registration answering each flag from `paths_by_flag`. A flag with no entry
        500s - which is what a link ESI will not join looks like - and 500 rather than 503 because the
        client retries the latter and a test must not sleep in backoff to prove one cell can fail."""
        def handler(call):
            path = paths_by_flag.get(call.query.get("flag"))
            if path is None:
                raise http_error(call.url, 500, {"error": "no route through the requested space"})
            return path

        self.env.server.get(f"/latest/route/{origin}/{destination}", handler=handler)

    # -- reading what came back ---------------------------------------------

    def run_cli(self, *argv):
        return self.env.run(["system", *argv])

    def refuses(self, *argv) -> str:
        code, stdout, stderr = self.env.run(list(argv))
        self.assertEqual(1, code, f"{argv} exited {code}: {stdout!r}")
        self.assertEqual("", stdout)
        self.assertIn("error:", stderr)
        return stderr

    def json_doc(self, *argv) -> dict:
        code, stdout, stderr = self.run_cli(*argv, "--json")
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def cells(self, stdout: str, name: str) -> list[str]:
        """One table row split into its cells, so a test can assert on a column rather than on the
        padding between columns."""
        line = next(line for line in stdout.splitlines() if line.startswith(name))
        return re.split(r"\s{2,}", line.strip())

    def csv_rows(self, *argv) -> list[dict]:
        code, stdout, stderr = self.run_cli(*argv, "--csv")
        self.assertEqual(0, code, stderr)
        return {row["name"]: row for row in csv.DictReader(io.StringIO(stdout))}


# ---------------------------------------------------------------------------
class ReportTests(SystemCommandTestCase):

    def test_the_table_prints_true_and_displayed_security_side_by_side(self):
        """The whole point of the row: `0.4488` and `0.4` are both printed, and the class comes from
        the true figure, so Enderailen reads lowsec under a displayed 0.4."""
        code, stdout, stderr = self.run_cli("Enderailen", "Kulelen")
        self.assertEqual(0, code, stderr)
        self.assertEqual(["system", "security", "shown", "class", "region", "planets", "planet types"],
                         re.split(r"\s{2,}", stdout.splitlines()[0].strip()))
        enderailen, kulelen = self.cells(stdout, "Enderailen"), self.cells(stdout, "Kulelen")
        self.assertEqual(["0.4488", "0.4", "lowsec"], enderailen[1:4])
        self.assertEqual(["0.4754", "0.5", "highsec"], kulelen[1:4])

    def test_a_report_that_straddles_a_class_line_says_so(self):
        """Two candidates a rounding step apart are the mistake this command was written against, so
        the note names both and states they are not one class - and says nothing at all when the run
        is nowhere near a line."""
        _, straddling, _ = self.run_cli("Enderailen", "Kulelen")
        self.assertIn("class line", straddling)
        self.assertIn("not all one class", straddling)
        self.assertIn("0.0266", straddling)         # the true spread between them, four decimals
        _, quiet, _ = self.run_cli("Rairomon")
        self.assertNotIn("class line", quiet)

    def test_planet_types_come_from_the_census_biggest_first(self):
        """The column answers "what is this system mostly made of", so it is ordered by count and the
        printed total is the same number its own breakdown adds up to."""
        planets = self.json_doc("Rairomon")["systems"][0]["planets"]
        self.assertEqual(11, planets["total"])
        self.assertEqual([("Barren", 3), ("Plasma", 2), ("Storm", 2), ("Temperate", 2),
                          ("Lava", 1), ("Oceanic", 1)],
                         [(entry["name"], entry["count"]) for entry in planets["types"]])

    def test_a_planet_type_nobody_can_colonise_is_still_named(self):
        """A Shattered planet is not an extractor's problem but it is a planet in the system being
        weighed, so it gets its name out of the census and its share of the total."""
        planets = self.json_doc(SHATTERED_NAME)["systems"][0]["planets"]
        self.assertEqual([("Shattered", 4), ("Barren", 1)],
                         [(entry["name"], entry["count"]) for entry in planets["types"]])
        self.assertEqual(5, planets["total"])

    def test_a_system_the_census_has_no_row_for_is_reported_as_an_answer(self):
        """402 of the SDE's 8,490 systems have no planet at all, so an absent row is a fact about New
        Eden and not a broken document: nothing is invented for it, and the note says where to look."""
        doc = self.json_doc("Zarzakh")
        self.assertEqual({"total": None, "census": None, "esi": None, "types": []},
                         doc["systems"][0]["planets"])
        self.assertIn("no row in the local census", "\n".join(doc["notes"]))
        self.assertEqual("-", self.cells(self.run_cli("Zarzakh")[1], "Zarzakh")[5])

    def test_the_census_count_is_printed_and_esi_s_is_kept_for_the_csv(self):
        """The printed figure is the census total so that it sums to the breakdown beside it, while
        both raw counts survive as their own columns - which is how a reader can tell an SDE build
        from a live count without asking for `--json`."""
        row = self.csv_rows("Rairomon")["Rairomon"]
        self.assertEqual("11", row["planets"])
        self.assertEqual("11", row["census_planets"])
        self.assertEqual("11", row["esi_planets"])

    def test_the_two_planet_sources_disagreeing_is_reported_with_the_build(self):
        """The census is a build and ESI is now; when they disagree the report has to say which said
        what, because that difference is an SDE update the reader should go and run."""
        self.env.server.get(f"/universe/systems/{RAIROMON}",
                            doc=system_document(RAIROMON, planets=12))
        doc = self.json_doc("Rairomon")
        planets = doc["systems"][0]["planets"]
        self.assertEqual((11, 12, 11), (planets["census"], planets["esi"], planets["total"]))
        note = next(n for n in doc["notes"] if "ESI counts 12 planets" in n)
        self.assertIn("SDE build 3494416", note)
        self.assertIn("eve-skills update-data", note)

    def test_an_unknown_system_names_the_input_and_stops(self):
        self.assertIn("NotASystemName", self.refuses("system", "NotASystemName"))

    def test_a_system_specified_twice_is_printed_once(self):
        """`system Rairomon Rairomon 30002772` is one system asked three ways, which is what a pasted
        list or a shell glob produces; repeating the row would double its planets in a reader's sum."""
        code, stdout, stderr = self.run_cli("Rairomon", "Rairomon", str(RAIROMON))
        self.assertEqual(0, code, stderr)
        self.assertEqual(1, len([line for line in stdout.splitlines() if line.startswith("Rairomon")]))
        self.assertEqual(1, len(self.env.server.calls_to(f"/universe/systems/{RAIROMON}")))

    def test_a_missing_census_refuses_before_any_request(self):
        """The census is what makes the planet half of this report work offline; with nothing
        installed the refusal has to name `update-data` and cost the run no ESI traffic at all."""
        os.remove(os.path.join(self.env.data_home, "eve-skills", "system_planets.json"))
        with mock.patch.object(alphadata, "PACKAGE_DATA_DIR", Path(tempfile.mkdtemp())):
            stderr = self.refuses("system", "Rairomon")
        self.assertIn("no local planet census", stderr)
        self.assertIn("eve-skills update-data", stderr)
        self.assertEqual([], self.env.server.calls)

    def test_a_census_missing_its_sections_names_the_same_fix(self):
        """`alphadata` raises ValueError for a document without `systems`; that has to arrive as the
        same one-line refusal rather than as a traceback, because it is the same user error."""
        self.write_census({"source": "synthetic", "build": 3494416})
        stderr = self.refuses("system", "Rairomon")
        self.assertIn("not in the expected format", stderr)
        self.assertIn("eve-skills update-data", stderr)

    def test_a_census_older_than_every_other_document_tolerates_is_flagged(self):
        """A census is only worth reading as a planet list while it tracks the SDE; four hundred days
        of drift has to be said out loud on every output mode, with the command that fixes it."""
        self.write_census({**CENSUS, "fetched": iso(-400 * 86400)})
        doc = self.json_doc("Rairomon")
        note = next(n for n in doc["notes"] if "days old" in n)
        self.assertIn("3494416", note)
        self.assertIn("eve-skills update-data", note)


# ---------------------------------------------------------------------------
class RouteTests(SystemCommandTestCase):

    def setUp(self):
        super().setUp()
        # Jita is 3 jumps from Rairomon by `shortest` and 5 by `secure`; the same 6 jumps either way
        # from Enderailen but along different roads; and ESI will not route from Zarzakh at all.
        self.route(RAIROMON, SYSTEM_FORGE, shortest=[RAIROMON, 30000145, 30000146, SYSTEM_FORGE],
                   secure=[RAIROMON, 30000150, 30000151, 30000152, 30000153, SYSTEM_FORGE])
        self.route(ENDERAELEN, SYSTEM_FORGE,
                   shortest=[ENDERAELEN, 30002768, 30002767, 30002766, 30002765, 30002764, SYSTEM_FORGE],
                   secure=[ENDERAELEN, 30002760, 30002761, 30002762, 30002763, 30002759, SYSTEM_FORGE])

    def test_a_route_column_names_the_hub_and_quotes_the_other_flag_beside_it(self):
        """`shortest` is what a pilot plans with and `secure` is what a hauler can actually fly, so
        the cell carries both numbers rather than the one number that flatters the candidate."""
        code, stdout, stderr = self.run_cli("Rairomon", "--route", "jita")
        self.assertEqual(0, code, stderr)
        self.assertIn("jumps to Jita", stdout)
        self.assertEqual("3 (secure 5)", self.cells(stdout, "Rairomon")[4])
        self.assertIn("the secure route to Jita is 5 jumps against 3 by shortest", stdout)

    def test_equal_jump_counts_on_different_paths_are_still_disclosed(self):
        """Two roads of the same length are invisible in a jump count, so they surface as a note - and
        equal counts on the same road say nothing, which is why Rairomon gets no such line."""
        doc = self.json_doc("Enderailen", "--route", "jita")
        routes = {entry["flag"]: entry for entry in doc["systems"][0]["routes"]}
        self.assertEqual(6, routes["shortest"]["jumps"])
        self.assertEqual(6, routes["secure"]["jumps"])
        self.assertNotEqual(routes["shortest"]["path"], routes["secure"]["path"])
        self.assertIn("different 6-jump path", "\n".join(doc["notes"]))

    def test_a_route_that_will_not_answer_costs_a_cell_and_not_the_report(self):
        """One unreachable link must not throw away the table somebody came for: the row prints a
        dash, and the note names the system, the flag and ESI's own reason."""
        code, stdout, stderr = self.run_cli("Rairomon", "Zarzakh", "--route", "jita")
        self.assertEqual(0, code, stderr)
        self.assertEqual("-", self.cells(stdout, "Zarzakh")[4])
        doc = self.json_doc("Rairomon", "Zarzakh", "--route", "jita")
        failed = next(entry for entry in doc["systems"][1]["routes"] if entry["error"])
        self.assertEqual("shortest", failed["flag"])
        self.assertIsNone(failed["jumps"])
        note = next(n for n in doc["notes"] if "no route answered for" in n)
        self.assertIn("Zarzakh shortest", note)

    def test_a_system_routed_to_itself_answers_locally(self):
        """`--route` a system you are already in is zero jumps, and asking ESI to route from a system
        to itself is a request the answer does not need."""
        code, stdout, stderr = self.run_cli("Jita", "--route", "jita")
        self.assertEqual(0, code, stderr)
        self.assertEqual("0", self.cells(stdout, "Jita")[4])
        self.assertEqual([], [call for call in self.env.server.calls if "/route/" in call.path])

    def test_the_hub_is_named_as_a_solar_system_not_as_a_station(self):
        """`--route jita` resolves through the hub table, whose label is a station; what was routed to
        is a solar system, and "jumps to Jita 4-4" would be a different question."""
        self.assertEqual({"system_id": SYSTEM_FORGE, "name": "Jita"},
                         {key: self.json_doc("Rairomon", "--route", "jita")["route_to"][key]
                          for key in ("system_id", "name")})

    def test_the_reported_flag_leads_and_its_sibling_becomes_the_comparison(self):
        """`--flag secure` is the same pair of facts read the other way round, for a hauler who wants
        the safe road's length as the headline."""
        doc = self.json_doc("Rairomon", "--route", "jita", "--flag", "secure")
        self.assertEqual(("secure", "shortest"),
                         (doc["route_to"]["flag"], doc["route_to"]["compared_flag"]))
        routes = {entry["flag"]: entry for entry in doc["systems"][0]["routes"]}
        self.assertEqual((5, 3), (routes["secure"]["jumps"], routes["shortest"]["jumps"]))

    def test_insecure_routes_are_reported_without_a_comparison(self):
        """`insecure` answers a different question and has no meaningful sibling, so the run fetches
        one route and compares nothing instead of paying for a number nobody asked for."""
        self.route(RAIROMON, SYSTEM_FORGE, insecure=[RAIROMON, 30000190, SYSTEM_FORGE])
        doc = self.json_doc("Rairomon", "--route", "jita", "--flag", "insecure")
        self.assertIsNone(doc["route_to"]["compared_flag"])
        self.assertEqual([{"flag": "insecure", "jumps": 2,
                           "path": [RAIROMON, 30000190, SYSTEM_FORGE], "error": None}],
                         doc["systems"][0]["routes"])

    def test_the_route_path_survives_in_json_for_every_flag(self):
        """A jump count alone cannot be checked against a planner's own route, so the systems walked
        are kept for both flags even though the table prints only the number."""
        routes = self.json_doc("Rairomon", "--route", "jita")["systems"][0]["routes"]
        self.assertEqual([["shortest", 3], ["secure", 5]],
                         [[entry["flag"], entry["jumps"]] for entry in routes])
        self.assertTrue(all(len(entry["path"]) == entry["jumps"] + 1 for entry in routes))

    def test_route_columns_exist_only_on_a_run_that_asked_for_a_route(self):
        """An empty jump column reads as zero jumps to anything parsing the CSV, so a report with no
        route has no route columns at all - and a report with one names the hub, the flag and both
        counts."""
        plain = self.csv_rows("Rairomon")["Rairomon"]
        self.assertNotIn("jumps", plain)
        routed = self.csv_rows("Rairomon", "--route", "jita")["Rairomon"]
        self.assertEqual(("30000142", "Jita", "shortest", "3", "secure", "5"),
                         (routed["route_to_id"], routed["route_to"], routed["route_flag"],
                          routed["jumps"], routed["compared_flag"], routed["compared_jumps"]))

    def test_an_unreachable_system_record_refuses_the_report_naming_it(self):
        """Security status has exactly one source, so a system that will not describe itself is not a
        row of dashes: the report cannot be made, and the id in the error has to be the one asked for."""
        self.env.server.get(f"/universe/systems/{RAIROMON}", error=(500, {"error": "shard unavailable"}))
        stderr = self.refuses("system", "Rairomon")
        self.assertIn("no solar system record", stderr)
        self.assertIn("Rairomon", stderr)


# ---------------------------------------------------------------------------
class OutputShapeTests(SystemCommandTestCase):

    def test_json_carries_the_provenance_of_every_figure(self):
        """Security and region are live ESI, planets are a local SDE build; a script comparing builds
        needs both stamps to know which of them it is looking at."""
        doc = self.json_doc("Rairomon")
        self.assertEqual(3494416, doc["sde_build"])
        self.assertEqual("2026-09-04", doc["census_fetched"])
        self.assertIsNone(doc["route_to"])
        row = doc["systems"][0]
        self.assertEqual(RAIROMON, row["system_id"])
        self.assertEqual(0.6293596029281616, row["security_status"])
        self.assertEqual(0.6, row["displayed_security"])
        self.assertEqual("high", row["class"])
        self.assertEqual({"constellation": "Kantanen", "region_id": CITADEL, "region": "The Citadel"},
                         {key: row[key] for key in ("constellation", "region_id", "region")})

    def test_json_keeps_the_notes_the_table_prints(self):
        """Every footnote is data too: a machine reading this report must not be able to miss that the
        class rule came from CCP's guide or that two sources were merged."""
        notes = "\n".join(self.json_doc("Enderailen", "Kulelen")["notes"])
        self.assertIn("developers.eveonline.com/docs/guides/system-security", notes)
        self.assertIn("local SDE census (build 3494416)", notes)

    def test_csv_is_parseable_and_holds_every_column_the_table_shows(self):
        """The CSV is the same report for a spreadsheet: security at the table's four decimals, the
        class as its raw key, and the planet breakdown in one cell so the row count stays one."""
        rows = self.csv_rows("Enderailen", "Kulelen")
        self.assertEqual(2, len(rows))
        enderailen = rows["Enderailen"]
        self.assertEqual(("0.4488", "0.4", "low"), (enderailen["security_status"],
                                                    enderailen["displayed_security"],
                                                    enderailen["class"]))
        self.assertEqual("high", rows["Kulelen"]["class"])
        self.assertEqual("Gas x5; Storm x2; Ice x1; Lava x1", enderailen["planet_types"])
        self.assertEqual("The Citadel", enderailen["region"])

    def test_csv_keeps_its_notes_on_stderr(self):
        """Notes are prose about the run; redirecting the rows to a file must not drag them in, or
        every consumer has to strip footnotes out of its own spreadsheet."""
        code, stdout, stderr = self.run_cli("Enderailen", "Kulelen", "--csv")
        self.assertEqual(0, code)
        self.assertNotIn("class line", stdout)
        self.assertIn("class line", stderr)


if __name__ == "__main__":
    unittest.main()
