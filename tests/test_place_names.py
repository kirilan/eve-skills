"""Station names for the markets report and the dashboard, player structures included."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from eve_skills import cmd_ledger, esi as esi_mod, universe

JITA = 60003760
KEEPSTAR = 1040000000001
HIDDEN = 1040000000002


class FakeClient:
    def __init__(self):
        self.structure_calls = []

    def get(self, path, token=None):
        ident = int(path.rsplit("/", 1)[-1])
        self.structure_calls.append(ident)
        if ident == KEEPSTAR and token == "good":
            return {"name": "X-EXMPL - Example Market Hub", "solar_system_id": 30000001}
        raise esi_mod.EsiError("403 forbidden")


def _op(hubs=(), facilities=()):
    return SimpleNamespace(hubs={h.label: h for h in hubs}, facilities={f.name: f for f in facilities})


class PlaceNamesTest(unittest.TestCase):
    def setUp(self):
        cmd_ledger._STRUCTURE_NAMES.clear()
        self.client = FakeClient()
        chars = [{"character_id": 1}, {"character_id": 2}]
        tokens = {1: {"access_token": "bad", "scopes": [universe.STRUCTURE_SCOPE]},
                  2: {"access_token": "good", "scopes": [universe.STRUCTURE_SCOPE]}}
        hub = SimpleNamespace(label="Hidden hub label", location_id=HIDDEN)
        self.patches = [
            mock.patch.object(cmd_ledger.esi_mod, "resolve_names", return_value={JITA: "Jita IV - Moon 4"}),
            mock.patch.object(cmd_ledger.sso, "list_characters", return_value=chars),
            mock.patch.object(cmd_ledger.sso, "get_access_token", side_effect=lambda cid: tokens[cid]),
            mock.patch.object(cmd_ledger.operation, "load", return_value=_op(hubs=[hub])),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_structures_named_by_esi_then_operation_label(self):
        names = cmd_ledger.place_names(self.client, [JITA, KEEPSTAR, HIDDEN, None])
        self.assertEqual(names[JITA], "Jita IV - Moon 4")
        self.assertEqual(names[KEEPSTAR], "X-EXMPL - Example Market Hub")
        self.assertEqual(names[HIDDEN], "Hidden hub label")

    def test_refusals_and_names_are_asked_once_per_process(self):
        cmd_ledger.place_names(self.client, [KEEPSTAR, HIDDEN])
        calls = len(self.client.structure_calls)
        again = cmd_ledger.place_names(self.client, [KEEPSTAR, HIDDEN])
        self.assertEqual(len(self.client.structure_calls), calls)
        self.assertEqual(again[KEEPSTAR], "X-EXMPL - Example Market Hub")
        self.assertEqual(again[HIDDEN], "Hidden hub label")

    def test_no_client_uses_operation_labels_only(self):
        names = cmd_ledger.place_names(None, [KEEPSTAR, HIDDEN])
        self.assertNotIn(KEEPSTAR, names)
        self.assertEqual(names[HIDDEN], "Hidden hub label")


if __name__ == "__main__":
    unittest.main()
