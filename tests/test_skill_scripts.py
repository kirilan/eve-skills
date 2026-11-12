"""The agent skills' helper scripts under .claude/skills/, run on canned CLI output.

Each script reaches the CLI only through its `cli()` function; these tests replace it with a table of
canned `--json` documents and check what the script prints for two shapes of operation file: a personal
site that invents in one facility and builds in another, and a one-facility corporation site with no
`planning` or `selling` block. Nothing here touches ESI, the network or the user's files.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import unittest
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / ".claude" / "skills"

LAB, FACTORY, OFFICE = 1_040_000_000_001, 1_040_000_000_002, 60_000_004
NOW = dt.datetime.now(dt.timezone.utc)


def iso(hours: float) -> str:
    return (NOW + dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def load(relative: str):
    path = SKILLS / relative
    spec = importlib.util.spec_from_file_location(f"skill_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCli:
    """Canned `eve-skills ... --json` documents, matched by the longest leading run of arguments."""

    def __init__(self, routes: dict[tuple, object]):
        self.routes = routes
        self.calls: list[tuple] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        best = max((key for key in self.routes if args[:len(key)] == key), key=len, default=None)
        if best is None:
            return None
        value = self.routes[best]
        return json.loads(json.dumps(value(args) if callable(value) else value))

    def called(self, *prefix: str) -> list[tuple]:
        return [c for c in self.calls if c[:len(prefix)] == prefix]


def run(module, argv: list[str]) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        module.main(argv)
    return out.getvalue(), err.getvalue()


# --- operation files -------------------------------------------------------------------------------------

def facility(system: str, label: str, location_id: int | None) -> dict:
    return {"system": system, "label": label, "location_id": location_id, "tax": None,
            "material_multiplier": {}, "time_multiplier": {}, "derived_from": None, "unknown": []}


def site(owner: str, fac: str, **extra) -> dict:
    base = {"owner": owner, "facility": fac, "facilities": {}, "characters": [], "readers": [], "hub": None,
            "freight_in": None, "freight_out": None, "seller": None, "pool_holder": None}
    base.update(extra)
    return base


PERSONAL_OP = {
    "file": "operation.json", "configured": True, "default_site": "home",
    "sites": {"home": site("personal", "lab", facilities={"manufacturing": "factory"},
                           characters=["Alice Builder", "Bob Inventor"], seller="Carol Hauler")},
    "facilities": {"lab": facility("X-EXMPL", "Example Lab", LAB),
                   "factory": facility("X-EXMPL", "Example Factory", FACTORY)},
    "hubs": {}, "freight": {},
    "planning": {"timezone": "UTC", "finish_windows": "08-10,20-22", "never_build": ["Large Example Rig II"],
                 "t1_copies": "invent"},
    "selling": {"keep": ["Datacore"]},
}

CORP_OP = {
    "file": "operation.json", "configured": True, "default_site": None,
    "sites": {"corp": site("corp", "office", readers=["Carol Hauler"])},
    "facilities": {"office": facility("Jita", "Example Office", OFFICE)},
    "hubs": {}, "freight": {},
    "planning": {"timezone": None, "finish_windows": None, "never_build": [], "t1_copies": "both"},
    "selling": {"keep": []},
}

NO_OP = {"file": "operation.json", "configured": False, "default_site": None, "sites": {}, "facilities": {},
         "hubs": {}, "freight": {}, "planning": {"timezone": None, "finish_windows": None, "never_build": [],
                                                 "t1_copies": "both"}, "selling": {"keep": []}}


# --- job board -------------------------------------------------------------------------------------------

def slots(used: int, top: int) -> dict:
    return {"used": used, "ready": 0, "max": top, "free": top - used, "next_end": iso(5) if used else None}


def character(cid: int, name: str) -> dict:
    return {"character_id": cid, "character_name": name, "complete": True, "next_end": None,
            "documents": {}, "slots": {"manufacturing": slots(2, 10), "science": slots(1, 10)}}


def blueprint(product: str, kind: str, location_id: int, *, activities, invents=(), runs=10, count=1,
              me=0, te=0, **extra) -> dict:
    row = {"blueprint_type_id": 1, "blueprint": f"{product} Blueprint", "product_type_id": 2, "product": product,
           "location_id": location_id, "location": f"location {location_id}", "division_number": None,
           "division": None, "kind": kind, "runs_per_job": runs if kind == "BPC" else None, "me": me, "te": te,
           "count": count, "can_build_jobs": None, "limiting_type_id": None, "limiting_material": None,
           "have": None, "per_job": None, "activities": list(activities), "invents": list(invents)}
    row.update(extra)
    return row


BUILD_ONLY = ("manufacturing",)
BUILD_OR_INVENT = ("manufacturing", "invention")
ORIGINAL = ("manufacturing", "copying", "material efficiency research", "time efficiency research")
DOCS = {"blueprints": {"last_modified": iso(-0.5), "expires": iso(0.5)},
        "assets": {"last_modified": iso(-0.25), "expires": iso(0.75)}}


def personal_status() -> dict:
    alice = {"owner_id": 1, "owner_name": "Alice Builder", "read_by_character_id": None, "documents": DOCS,
             "characters": [character(1, "Alice Builder")],
             "jobs": {"ready": [], "running": [
                 {"installer_id": 1, "installer_name": "Alice Builder", "activity_id": 1,
                  "activity": "manufacturing", "product_type_id": 3, "product": "Small Auxiliary Thrusters II",
                  "runs": "4 x1", "count": 1, "end_start": iso(3), "end_end": None}]},
             "blueprints": [
                 # an invented copy sitting at the lab: it has to travel to the factory first
                 blueprint("Small Auxiliary Thrusters II", "BPC", LAB, activities=BUILD_ONLY, count=2, me=2, te=4,
                           can_build_jobs=0, limiting_material="Morphite", have=0, per_job=5),
                 # a copy that can build or invent: `t1_copies: invent` lists it for invention only
                 blueprint("Small Auxiliary Thrusters I", "BPC", LAB, activities=BUILD_OR_INVENT,
                           invents=["Small Auxiliary Thrusters II"], count=3),
                 # a line with no job in the history window: hidden
                 blueprint("Small Anti-EM Pump I", "BPC", LAB, activities=BUILD_OR_INVENT,
                           invents=["Small Anti-EM Pump II"], count=4),
                 # a product the planning block says never to build
                 blueprint("Large Example Rig I", "BPC", LAB, activities=BUILD_OR_INVENT,
                           invents=["Large Example Rig II"]),
                 # somewhere else entirely
                 blueprint("Small Auxiliary Thrusters II", "BPC", 60_000_005, activities=BUILD_ONLY)],
             "blueprint_adjustments": {"dropped": 1, "reduced": 0},
             "warnings": [{"owner_id": 1, "subject": "blueprint_adjustments", "dropped": 1, "reduced": 0,
                           "snapshot": iso(-0.5),
                           "message": "1 copies dropped and 0 reduced: used by jobs delivered after the "
                                      "blueprint snapshot (06:00 UTC)"}]}
    bob = {"owner_id": 2, "owner_name": "Bob Inventor", "read_by_character_id": None, "documents": DOCS,
           "characters": [character(2, "Bob Inventor")], "jobs": {"ready": [], "running": []},
           "blueprints": [blueprint("Small Auxiliary Thrusters II", "BPC", FACTORY, activities=BUILD_ONLY,
                                    count=1, me=2, te=4)],
           "blueprint_adjustments": {"dropped": 0, "reduced": 0}, "warnings": []}
    stranger = {"owner_id": 3, "owner_name": "Dave Elsewhere", "read_by_character_id": None, "documents": {},
                "characters": [character(3, "Dave Elsewhere")], "jobs": {"ready": [], "running": []},
                "blueprints": [], "blueprint_adjustments": {}, "warnings": []}
    return {"owner_kind": "character", "hints": [], "warnings": [], "owners": [alice, bob, stranger]}


def time_row(activity: str, product: str, installer: str, fac: str, fac_id: int, hours: float, *,
             scaled: str | None = None, fits=()) -> dict:
    return {"activity": activity, "product": product, "installer": installer, "facility_id": fac_id,
            "facility": fac, "hours_per_run": hours, "min_hours": hours, "max_hours": hours, "jobs": 3,
            "scaled_from": scaled, "fits": [{"runs": n, "end": iso(h)} for n, h in fits]}


def personal_times(_args) -> dict:
    return {"days": 14, "time_zone": "UTC", "site": "home", "hints": [], "warnings": [], "documents": [
        {"owner": "Alice Builder", "corporation_id": None, "cache": None, "times": [
            time_row("manufacturing", "Small Auxiliary Thrusters II", "Alice Builder", "factory", FACTORY, 1.5,
                     fits=[(4, 6)])]},
        {"owner": "Bob Inventor", "corporation_id": None, "cache": None, "times": [
            time_row("invention", "Small Auxiliary Thrusters II Blueprint", "Bob Inventor", "lab", LAB, 1.0,
                     scaled="old-lab", fits=[(8, 8)])]}]}


def item(name: str, qty: int, location_id: int, *, flag: str = "Hangar", category: str = "Material",
         group: str = "Construction Components", division: int | None = None) -> dict:
    return {"item_id": 9, "type_id": 9, "name": name, "type_name": name, "custom_name": None, "group_name": group,
            "category_name": category, "quantity": qty, "singleton": False, "flag": flag,
            "division_number": division, "division": f"Division {division}" if division else None,
            "location_id": location_id, "location_kind": "structure",
            "location_path": [{"id": location_id, "name": "somewhere", "kind": "structure"}],
            "unit_price": None, "value": None}


def inventory(*items: dict) -> dict:
    return {"characters": [{"character_id": 1, "name": "x", "items": list(items)}]}


def personal_routes() -> dict:
    return {
        ("operation",): PERSONAL_OP,
        ("industry", "status"): personal_status(),
        ("jobs", "--times"): personal_times,
        ("jobs", "--since"): {"since": iso(-2), "time_zone": "UTC", "site": "home", "hints": [], "warnings": [],
                              "documents": [{"owner": "Alice Builder", "corporation_id": None, "cache": {},
                                             "jobs": [{"installer": "Alice Builder", "installer_id": 1,
                                                       "activity": "manufacturing",
                                                       "product": "Small Auxiliary Thrusters II", "product_id": 3,
                                                       "runs": 4, "count": 1, "facility_id": FACTORY,
                                                       "facility": "factory", "blueprint_ids": [7],
                                                       "start": iso(-1), "end": iso(3),
                                                       "status": "1 active"}]}]},
        ("inventory", "--char", "Alice Builder"): inventory(
            item("Morphite", 40, FACTORY, group="Mineral", category="Material"),
            item("Datacore - Example Physics", 8, LAB, category="Commodity")),
        ("inventory", "--char", "Bob Inventor"): inventory(
            item("Robotics", 60, FACTORY, flag="Deliveries", category="Commodity"),
            item("Construction Blocks", 500, FACTORY, category="Commodity")),
        ("restock",): {"lines": [{"kind": "build", "label": "Small Auxiliary Thrusters II ME2 x10r @Bob Inventor",
                                  "units": 30, "fit": 20, "status": "partial", "haul_m3": 0.0}],
                       "materials": [
                           {"type_id": 1, "name": "Morphite", "need": 150, "on_site": 40, "buy": 110},
                           {"type_id": 2, "name": "Robotics", "need": 90, "on_site": 60, "buy": 30},
                           {"type_id": 3, "name": "Construction Blocks", "need": 300, "on_site": 500, "buy": 0}],
                       "transfers": [{"from": "Alice Builder", "to": "Bob Inventor", "type_id": 1,
                                      "quantity": 40, "name": "Morphite"}]},
    }


class JobBoardPersonalSiteTests(unittest.TestCase):
    def setUp(self):
        self.board = load("industry-jobs/job_board.py")
        self.cli = FakeCli(personal_routes())
        self.board.cli = self.cli
        self.out, _ = run(self.board, ["--since", "08:00"])

    def test_header_names_the_facility_of_each_activity(self):
        self.assertIn("manufacturing at Example Factory", self.out)
        self.assertIn("invention/copying/ME research/TE research at Example Lab", self.out)
        self.assertNotIn("Dave Elsewhere", self.out)  # not a character of this site

    def test_copies_by_what_they_can_do(self):
        build = self.out.split("## Copies to build")[1].split("## Copies to invent")[0]
        invent = self.out.split("## Copies to invent from")[1].split("\n\n")[0]
        self.assertIn("Small Auxiliary Thrusters II", build)
        self.assertNotIn("Small Auxiliary Thrusters I ", build)          # t1_copies: invent
        self.assertIn("Small Auxiliary Thrusters I ", invent)
        self.assertIn("-> Small Auxiliary Thrusters II", invent)
        self.assertNotIn("Anti-EM", self.out)                              # no job for that line
        self.assertIn("1 copies to invent from", self.out)                 # ... counted as hidden
        self.assertIn("left out by planning.never_build: Large Example Rig II", self.out)
        self.assertIn("1 blueprint rows are held away", self.out)

    def test_a_copy_away_from_its_activity_says_where_to_haul_it(self):
        lines = [line for line in self.out.splitlines() if "Small Auxiliary Thrusters II " in line and " x " in line]
        at_lab = [line for line in lines if "Alice Builder" in line]
        at_factory = [line for line in lines if "Bob Inventor" in line]
        self.assertIn("[Example Lab; haul to Example Factory first]", at_lab[0])
        self.assertIn("[Example Factory]", at_factory[0])
        # the copy's own hangar check means nothing on a personal site
        self.assertNotIn("limit Morphite", self.out)
        self.assertNotIn("buildable jobs", self.out)

    def test_sizing_uses_measured_times_and_marks_converted_ones(self):
        self.assertIn("Alice Builder 1.50h: 4 runs ->", self.out)
        self.assertIn("Bob Inventor ~1.00h: 8 runs ->", self.out)
        times = self.cli.called("jobs", "--times")[0]
        self.assertEqual(times[times.index("--site") + 1], "home")
        self.assertEqual(times[times.index("--finish-window") + 1], "08-10,20-22")

    def test_pool_check_lists_every_short_input(self):
        self.assertIn("## Pool check: every copy to build, installed by Bob Inventor", self.out)
        self.assertIn("covered 20/30", self.out)
        short = self.out.split("short (need / on site / short):")[1].split("trades")[0]
        self.assertIn("Morphite", short)
        self.assertIn("Robotics", short)
        self.assertNotIn("Construction Blocks", short)
        restock = self.cli.called("restock")
        self.assertEqual(len(restock), 1)
        self.assertIn("Small Auxiliary Thrusters II=30:2:10@Bob Inventor", restock[0])
        self.assertNotIn("--invent", restock[0])  # attempts are a planning decision: printed, not run
        self.assertIn('--invent "Small Auxiliary Thrusters II=30@Alice Builder"', self.out)

    def test_inputs_per_facility_and_the_deliveries_hangar(self):
        factory = self.out.split("## Inputs at Example Factory")[1].split("##")[0]
        self.assertIn("Bob Inventor: 60", factory)
        self.assertIn("Alice Builder: 40", factory)
        self.assertLess(factory.index("Robotics"), factory.index("Morphite"))  # minerals last
        self.assertIn("Bob Inventor: ESI lists 1 of these 2 items in the Deliveries hangar (Robotics)", factory)
        self.assertIn("## Inputs at Example Lab", self.out)

    def test_header_warnings_and_installed_since(self):
        self.assertIn("warning: 1 copies dropped and 0 reduced", self.out)
        self.assertIn("## Installed since", self.out)
        self.assertIn("Small Auxiliary Thrusters II (4) at factory, 1 active", self.out)

    def test_a_pool_holder_in_the_file_wins(self):
        op = json.loads(json.dumps(PERSONAL_OP))
        op["sites"]["home"]["pool_holder"] = "Alice Builder"
        self.board.cli = cli = FakeCli({**personal_routes(), ("operation",): op})
        out, _ = run(self.board, [])
        self.assertIn("installed by Alice Builder", out)
        self.assertIn("@Alice Builder", cli.called("restock")[0][-1])


def corp_status() -> dict:
    owner = {"owner_id": 98_000_001, "owner_name": "Example Corp", "read_by_character_id": 5,
             "documents": DOCS, "characters": [character(5, "Carol Hauler")],
             "jobs": {"ready": [], "running": []},
             "blueprints": [
                 blueprint("Small Auxiliary Thrusters II", "BPC", OFFICE, activities=BUILD_ONLY, count=2, me=2,
                           division_number=1, can_build_jobs=0, limiting_material="Morphite", have=0, per_job=5),
                 blueprint("Small Auxiliary Thrusters I", "BPO", OFFICE, activities=ORIGINAL,
                           invents=["Small Auxiliary Thrusters II"], division_number=1)],
             "blueprint_adjustments": {"dropped": 0, "reduced": 0}, "warnings": []}
    other = dict(owner, owner_name="Another Corp", characters=[character(6, "Erin Other")], blueprints=[])
    return {"owner_kind": "corporation", "hints": [], "warnings": [], "owners": [other, owner]}


def corp_routes() -> dict:
    return {
        ("operation",): CORP_OP,
        ("industry", "status", "--corp"): corp_status(),
        ("jobs", "--times"): {"days": 14, "documents": [{"owner": "Example Corp", "times": [
            time_row("manufacturing", "Small Auxiliary Thrusters II", "Carol Hauler", "office", OFFICE, 2.0),
            time_row("copying", "Small Auxiliary Thrusters I Blueprint", "Carol Hauler", "office", OFFICE, 0.5)]}]},
        ("inventory", "--corp"): inventory(item("Morphite", 12, OFFICE, group="Mineral", division=1),
                                           item("Robotics", 7, OFFICE, category="Commodity", division=2)),
    }


class JobBoardCorpSiteTests(unittest.TestCase):
    def setUp(self):
        self.board = load("industry-jobs/job_board.py")
        self.cli = FakeCli(corp_routes())
        self.board.cli = self.cli
        self.out, _ = run(self.board, [])

    def test_reads_the_corporation_of_the_sites_reader(self):
        self.assertIn("site corp (corp), read via Carol Hauler", self.out)
        self.assertIn("all activities", self.out.replace(
            "manufacturing/invention/copying/ME research/TE research", "all activities"))
        self.assertNotIn("Erin Other", self.out)

    def test_no_planning_block_means_utc_and_end_times_only(self):
        self.assertIn("UTC", self.out.splitlines()[0])
        times = self.cli.called("jobs", "--times")[0]
        self.assertNotIn("--finish-window", times)
        self.assertIn("No finish windows set", self.out)
        self.assertIn("Carol Hauler 2.00h: 10 runs ->", self.out)
        self.assertNotIn("outside window", self.out)

    def test_corp_hangar_check_restock_command_and_divisions(self):
        self.assertIn("buildable jobs 0; limit Morphite 0/5", self.out)
        self.assertNotIn("## Pool check", self.out)
        self.assertEqual(self.cli.called("restock"), [])
        self.assertIn('restock --site corp "Small Auxiliary Thrusters II=20:2:10"', self.out)
        self.assertIn("## Originals (copy or build)", self.out)
        self.assertIn("copy: Carol Hauler 0.50h", self.out)
        self.assertIn("div 1: 12", self.out)
        self.assertIn("div 2: 7", self.out)
        self.assertEqual(self.cli.called("inventory")[0][:4], ("inventory", "--corp", "--char", "Carol Hauler"))


class JobBoardWithoutOperationFileTests(unittest.TestCase):
    def test_says_what_it_needs(self):
        board = load("industry-jobs/job_board.py")
        board.cli = FakeCli({("operation",): NO_OP})
        with self.assertRaises(SystemExit) as caught:
            run(board, [])
        self.assertIn("eve-skills operation template", str(caught.exception.code))

    def test_unknown_site(self):
        board = load("industry-jobs/job_board.py")
        board.cli = FakeCli({("operation",): PERSONAL_OP})
        with self.assertRaises(SystemExit) as caught:
            run(board, ["--site", "moon"])
        self.assertIn("no site 'moon'", str(caught.exception.code))


if __name__ == "__main__":
    unittest.main()
