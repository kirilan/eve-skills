"""docs/agent-reference.md stays true: its ledger tables and columns match what `ledger_db` creates."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import unittest

from eve_skills import ledger_db

REFERENCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "agent-reference.md")


def documented_tables(text: str) -> dict[str, list[str]]:
    """{table: [columns]} from the `| \`table\` | \`col\`, ... |` rows of the ledger section."""
    section = text.split("## 3. Ledger tables", 1)[1].split("\n## ", 1)[0]
    tables = {}
    for line in section.splitlines():
        match = re.match(r"\|\s*`([a-z_]+)`\s*\|(.*)\|\s*$", line)
        if match:
            tables[match.group(1)] = re.findall(r"`([a-z_0-9]+)`", match.group(2))
    return tables


def created_tables() -> dict[str, list[str]]:
    with tempfile.TemporaryDirectory() as folder:
        with contextlib.closing(ledger_db.connect(os.path.join(folder, "ledger.sqlite3"))) as conn:
            names = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            return {name: [col[1] for col in conn.execute(f'PRAGMA table_info("{name}")')] for name in names}


class AgentReferenceTests(unittest.TestCase):
    def setUp(self):
        with open(REFERENCE, encoding="utf-8") as fh:
            self.documented = documented_tables(fh.read())
        self.created = created_tables()

    def test_the_section_parses(self):
        self.assertIn("jobs", self.documented)
        self.assertIn("job_id", self.documented["jobs"])

    def test_every_documented_table_and_column_exists(self):
        for table, columns in self.documented.items():
            with self.subTest(table=table):
                self.assertIn(table, self.created, f"documented table {table} is not in the schema")
                missing = [c for c in columns if c not in self.created[table]]
                self.assertEqual([], missing, f"documented columns of {table} not in the schema")

    def test_every_table_and_column_is_documented(self):
        for table, columns in self.created.items():
            with self.subTest(table=table):
                self.assertIn(table, self.documented, f"table {table} is not documented")
                self.assertEqual(sorted(columns), sorted(self.documented[table]))


if __name__ == "__main__":
    unittest.main()
