import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from migration_ledger import (
    INVALID_EMAIL_REASON,
    NO_EMAIL_REASON,
    SHARED_EMAIL_REASON,
    approve_shared_emails,
    discover,
    email_reference,
    generate_batch,
    importable_links,
    migration_counts,
    normalize_email,
    open_ledger,
    record_batch_state,
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
        self.assertEqual(
            {item["source_contact_id"] for item in importable_links(db)}, {"11"}
        )
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
        item = json.loads(output.read_text())["unmatched_contact_exceptions"][0]
        self.assertEqual(item, {
            "activity_type": "Phone Call", "status": "unmatched_contact",
            "record_count": 1,
            "earliest_activity": "1970-01-01T00:00:01Z",
            "latest_activity": "1970-01-01T00:00:01Z",
        })

    def test_email_normalization_and_invalid_email_are_planned_unmatched(self):
        source = sqlite3.connect(self.source)
        source.execute("UPDATE JobAdderContacts SET email=' invalid @example.test' WHERE contactId=11")
        source.commit()
        source.close()
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        db = open_ledger(self.ledger)
        row = db.execute("SELECT * FROM activity_links WHERE source_contact_id='11'").fetchone()
        self.assertEqual(row["status"], "unmatched_contact")
        self.assertEqual(row["reason_code"], INVALID_EMAIL_REASON)
        self.assertEqual(list(importable_links(db)), [])
        db.close()
        self.assertEqual(normalize_email("  Person@Example.TEST "), "person@example.test")
        self.assertIsNone(normalize_email("person@example"))

    def test_shared_activity_creates_one_pair_and_csv_row_per_contact(self):
        source = sqlite3.connect(self.source)
        source.execute(
            "INSERT INTO JobAdderContacts VALUES (12, 'second@example.test', 1)"
        )
        source.execute("INSERT INTO JobAdderNoteContacts VALUES (1001, 12, 1)")
        source.commit()
        source.close()

        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        ledger = open_ledger(self.ledger)
        self.assertEqual(migration_counts(ledger), {
            "unique_source_activities": 2,
            "activity_contact_pairs": 3,
            "expected_hubspot_activity_creations": 2,
        })
        ledger.close()

        batch = Path(self.temp.name) / "batch.csv"
        manifest_path = Path(self.temp.name) / "batch.audit.json"
        generate_batch(self.source, self.ledger, batch, manifest_path)
        with batch.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["Contact email"] for row in rows},
            {"synthetic@example.test", "second@example.test"},
        )
        self.assertTrue(all(len(row) == 4 for row in rows))
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["manifest_type"], "non_imported_audit_manifest")
        self.assertTrue(manifest["batch_id"])
        self.assertEqual(len(manifest["csv_sha256"]), 64)
        self.assertTrue(all(len(row["row_sha256"]) == 64 for row in manifest["rows"]))
        self.assertEqual([row["csv_row_number"] for row in manifest["rows"]], [2, 3])
        self.assertEqual(
            {tuple(item["source_key"].values()) for item in manifest["rows"]},
            {(1, "101", "11"), (1, "101", "12")},
        )
        with self.assertRaises(FileExistsError):
            generate_batch(self.source, self.ledger, batch, manifest_path)

    def test_shared_normalized_email_requires_explicit_policy(self):
        source = sqlite3.connect(self.source)
        source.execute(
            "INSERT INTO JobAdderContacts VALUES (12, ' SYNTHETIC@EXAMPLE.TEST ', 1)"
        )
        source.execute("INSERT INTO JobAdderNoteContacts VALUES (1001, 12, 1)")
        source.commit()
        source.close()
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        ledger = open_ledger(self.ledger)
        shared = list(ledger.execute(
            "SELECT * FROM contact_email_plans WHERE status='shared_email_exception'"
        ))
        self.assertEqual({row["source_contact_id"] for row in shared}, {"11", "12"})
        self.assertTrue(all(row["reason_code"] == SHARED_EMAIL_REASON for row in shared))
        ledger.close()
        report = Path(self.temp.name) / "exceptions.json"
        write_report(self.ledger, report, "preview")
        exception = json.loads(report.read_text())["shared_email_exceptions"][0]
        self.assertEqual(exception["source_contact_ids"], "11,12")
        self.assertEqual(exception["email_sha256"], email_reference("synthetic@example.test"))
        batch = Path(self.temp.name) / "blocked.csv"
        manifest = Path(self.temp.name) / "blocked.json"
        with self.assertRaises(ValueError):
            generate_batch(self.source, self.ledger, batch, manifest)
        decisions = Path(self.temp.name) / "policy.csv"
        decisions.write_text(
            f"email_sha256,decision\n{exception['email_sha256']},approve_import\n"
        )
        with self.assertRaises(ValueError):
            approve_shared_emails(self.ledger, decisions, False)
        self.assertEqual(approve_shared_emails(self.ledger, decisions, True), 2)
        generate_batch(self.source, self.ledger, batch, manifest, "synthetic-batch")
        with batch.open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(
                {row["Contact email"] for row in csv.DictReader(handle)},
                {"synthetic@example.test"},
            )

    def test_batch_state_requires_order_and_import_id(self):
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        batch = Path(self.temp.name) / "batch.csv"
        manifest = Path(self.temp.name) / "manifest.json"
        generate_batch(self.source, self.ledger, batch, manifest, "batch-1")
        with self.assertRaises(ValueError):
            record_batch_state(self.ledger, "batch-1", "submitted", "import-1")
        record_batch_state(self.ledger, "batch-1", "reviewed")
        with self.assertRaises(ValueError):
            record_batch_state(self.ledger, "batch-1", "submitted")
        record_batch_state(self.ledger, "batch-1", "submitted", "import-1")
        record_batch_state(self.ledger, "batch-1", "imported", "import-1")
        ledger = open_ledger(self.ledger)
        self.assertEqual(ledger.execute(
            "SELECT status FROM batches WHERE batch_id='batch-1'"
        ).fetchone()[0], "imported")
        self.assertEqual({row[0] for row in ledger.execute(
            "SELECT state FROM batch_rows WHERE batch_id='batch-1'"
        )}, {"imported"})
        ledger.close()


if __name__ == "__main__":
    unittest.main()
