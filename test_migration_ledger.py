import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from migration_ledger import (
    NO_EMAIL_REASON,
    discover,
    importable_links,
    open_ledger,
    reconsider,
    write_report,
)
from hubspot_history_audit import open_read_only


class MigrationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source.db"
        self.ledger = root / "ledger.db"
        self.mapping = root / "mapping.csv"
        db = sqlite3.connect(self.source)
        db.executescript(
            """
            CREATE TABLE JobAdderContacts
              (contactId INTEGER, email TEXT, JobAdderOrganisationId INTEGER);
            CREATE TABLE JobAdderNotes
              (Id INTEGER, noteId INTEGER, type TEXT, source TEXT, text TEXT,
               createdAt INTEGER, JobAdderOrganisationId INTEGER);
            CREATE TABLE JobAdderNoteContacts
              (noteId INTEGER, contactId INTEGER, JobAdderOrganisationId INTEGER);
            CREATE TABLE JobAdderNoteTypes
              (name TEXT, JobAdderOrganisationId INTEGER);
            INSERT INTO JobAdderContacts VALUES (10, NULL, 1), (11, 'synthetic@example.test', 1);
            INSERT INTO JobAdderNotes VALUES
              (100, 1000, 'Phone Call', 'User', 'synthetic', 1000, 1),
              (101, 1001, 'Phone Call', 'User', 'synthetic', 2000, 1);
            INSERT INTO JobAdderNoteContacts VALUES (1000, 10, 1), (1001, 11, 1);
            """
        )
        db.commit()
        db.close()
        self.mapping.write_text(
            "jobadder_type,jobadder_source,classification,reason\n"
            "Phone Call,*,CALL,Synthetic mapping\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_no_email_link_is_ledgered_but_not_importable(self):
        counts = discover(
            self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1
        )
        self.assertEqual(counts, {"inserted": 2})
        db = open_ledger(self.ledger)
        row = db.execute(
            "SELECT * FROM activity_links WHERE source_contact_id = '10'"
        ).fetchone()
        self.assertEqual(row["status"], "unmatched_contact")
        self.assertEqual(row["reason_code"], NO_EMAIL_REASON)
        self.assertEqual(row["source_activity_id"], "100")
        self.assertEqual(row["mapping_decision"], "CALL")
        self.assertIsNone(row["hubspot_contact_id"])
        self.assertEqual(list(importable_links(db)), [])
        # Discovery did not mutate the source or add ledger tables to it.
        source = sqlite3.connect(f"{self.source.as_uri()}?mode=ro", uri=True)
        self.assertNotIn("activity_links", {r[0] for r in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )})
        source.close()
        db.close()

    def test_source_connection_has_both_read_only_protections(self):
        source = open_read_only(self.source)
        self.assertEqual(source.execute("PRAGMA query_only").fetchone()[0], 1)
        source.execute("PRAGMA query_only = OFF")
        with self.assertRaises(sqlite3.OperationalError):
            source.execute("CREATE TABLE forbidden_write (id INTEGER)")
        source.close()

    def test_reports_aggregate_unmatched_by_type_and_date_range(self):
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        output = Path(self.temp.name) / "preview.json"
        write_report(self.ledger, output, "preview")
        item = json.loads(output.read_text())["unmatched_contacts_without_source_email"][0]
        self.assertEqual(item, {
            "activity_type": "Phone Call", "status": "unmatched_contact",
            "record_count": 1,
            "earliest_activity": "1970-01-01T00:00:01Z",
            "latest_activity": "1970-01-01T00:00:01Z",
        })

    def test_reconsideration_requires_confirmation_and_existing_record_id(self):
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        matches = Path(self.temp.name) / "matches.csv"
        matches.write_text("source_contact_id,hubspot_contact_id\n10,9001\n")
        with self.assertRaises(ValueError):
            reconsider(self.ledger, matches, False)
        self.assertEqual(reconsider(self.ledger, matches, True), 1)
        db = open_ledger(self.ledger)
        rows = list(importable_links(db))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["hubspot_contact_id"], "9001")
        self.assertNotIn("email", rows[0].keys())
        db.close()
        output = Path(self.temp.name) / "reconciliation.json"
        write_report(self.ledger, output, "reconcile")
        summary = json.loads(output.read_text())[
            "unmatched_contacts_without_source_email"
        ]
        self.assertEqual(summary[0]["status"], "approved_contact_match")


if __name__ == "__main__":
    unittest.main()
