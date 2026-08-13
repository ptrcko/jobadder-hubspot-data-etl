import csv
import hashlib
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
    normalize_note_body,
    open_ledger,
    record_batch_state,
    reconcile_manual_import,
    record_stronger_confirmation,
    render_email_subject,
    write_report,
    trim_quoted_history,
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
               createdAt INTEGER, JobAdderOrganisationId INTEGER, subject TEXT);
            CREATE TABLE JobAdderNoteContacts
              (noteId INTEGER, contactId INTEGER, JobAdderOrganisationId INTEGER);
            CREATE TABLE JobAdderNoteTypes
              (name TEXT, JobAdderOrganisationId INTEGER);
            INSERT INTO JobAdderContacts VALUES (10, NULL, 1), (11, 'synthetic@example.test', 1);
            INSERT INTO JobAdderNotes
              (Id,noteId,type,source,text,createdAt,JobAdderOrganisationId) VALUES
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

    def test_body_normalization_preserves_multiline_unicode_and_lists(self):
        raw = "\r\n  First, \"quoted\"  \r\n\u00a0\r\n\r\n- café  \r\n- 東京\n\n"
        self.assertEqual(normalize_note_body(raw),
                         '  First, "quoted"\n\n- café\n- 東京')

    def test_email_subject_rendering_rule_preserves_source_content(self):
        body = 'Résumé, "quoted"\nSubject: déjà present\n東京'
        rendered = render_email_subject(body, '  Café, "Q3" 東京  ',
                                        "OUTBOUND_EMAIL")
        self.assertEqual(
            rendered,
            'Subject:   Café, "Q3" 東京  \n\n'
            'Résumé, "quoted"\nSubject: déjà present\n東京',
        )
        # Repeated subject text is source content, not a deduplication signal.
        self.assertEqual(rendered.count('Café, "Q3" 東京'), 1)
        repeated = render_email_subject("Same subject", "Same subject",
                                        "INBOUND_EMAIL")
        self.assertEqual(repeated, "Subject: Same subject\n\nSame subject")
        for subject in (None, "", " \t"):
            self.assertEqual(render_email_subject(body, subject, "INBOUND_EMAIL"), body)
        self.assertEqual(render_email_subject(body, "Ignored", "NOTE"), body)

    def test_quoted_history_requires_one_complete_header_block(self):
        raw = ("Newest reply\r\n\r\nFrom: Older Person\r\nSent: Monday\r\n"
               "To: Recipient\r\nCc: Copy\r\nSubject: Old subject\r\nOld text")
        retained, outcome, reason = trim_quoted_history(raw)
        self.assertEqual(normalize_note_body(retained), "Newest reply")
        self.assertEqual((outcome, reason),
                         ("trimmed", "single_complete_header_block"))
        prose = "A note\nFrom: this phrase is prose, not a mail header\nContinue"
        self.assertEqual(trim_quoted_history(prose),
                         (prose, "not_found", "no_complete_header_block"))
        ambiguous = raw + "\nFrom: Again\nDate: Today\nTo: X\nSubject: Y"
        self.assertEqual(trim_quoted_history(ambiguous)[1:],
                         ("review", "conflicting_quoted_history_boundaries"))

    def test_notes_csv_round_trip_and_strict_duplicate_ledger(self):
        self.mapping.write_text(
            "jobadder_type,jobadder_source,classification,reason\n"
            "Phone Call,*,OUTBOUND_EMAIL,Synthetic email-like mapping\n",
            encoding="utf-8",
        )
        db = sqlite3.connect(self.source)
        db.execute("UPDATE JobAdderNotes SET text=?, subject=? WHERE Id=101",
                   ('Newest, "reply"  \r\n\r\n\u00a0\r\n- café\r\n\r\n'
                    'From: Old\r\nSent: Yesterday\r\nTo: Person\r\nSubject: Earlier\r\nold',
                    'Résumé, "東京"'))
        db.execute("""INSERT INTO JobAdderNotes
                   (Id,noteId,type,source,text,createdAt,JobAdderOrganisationId,subject)
                   VALUES (102,1002,'Phone Call','User',?,2000,1,?)""",
                   ('Newest, "reply"\n\n- café', 'Résumé, "東京"'))
        db.execute("INSERT INTO JobAdderNoteContacts VALUES (1002,11,1)")
        db.commit(); db.close()
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        directory = Path(self.temp.name) / "notes"
        counts = generate_batch(self.source, self.ledger, directory, "notes-1")
        self.assertEqual(counts["strict_duplicates_excluded"], 1)
        with (directory / "notes.csv").open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, [
                "Email <CONTACT email>", "Note body <NOTE hs_note_body>",
                "Activity date <NOTE hs_timestamp>"])
            rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Note body <NOTE hs_note_body>"],
                         'Subject: Résumé, "東京"\n\nNewest, "reply"\n\n- café')
        self.assertEqual(rows[0]["Activity date <NOTE hs_timestamp>"],
                         "1970-01-01T00:00:02Z")
        ledger = open_ledger(self.ledger)
        duplicate = ledger.execute(
            "SELECT * FROM note_processing WHERE outcome='duplicate'").fetchone()
        self.assertEqual(duplicate["reason_code"],
                         "strict_contact_timestamp_content_match")
        self.assertEqual(len(duplicate["survivor_reference_sha256"]), 64)
        self.assertNotIn("Newest", dict(duplicate).values())
        ledger.close()
        manifest = json.loads((directory / "manifest.json").read_text())
        visible = rows[0]["Note body <NOTE hs_note_body>"]
        self.assertEqual(manifest["body_transformation_version"],
                         "note-body-v2-email-subject")
        self.assertEqual(manifest["rows"][0]["transformed_character_count"],
                         len(visible))
        self.assertEqual(manifest["rows"][0]["transformed_body_sha256"],
                         hashlib.sha256(visible.encode("utf-8")).hexdigest())

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

        batch_dir = Path(self.temp.name) / "batch"
        generate_batch(self.source, self.ledger, batch_dir)
        batch = batch_dir / "notes.csv"
        manifest_path = batch_dir / "manifest.json"
        with batch.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["Email <CONTACT email>"] for row in rows},
            {"synthetic@example.test", "second@example.test"},
        )
        self.assertEqual(list(rows[0]), ["Email <CONTACT email>", "Note body <NOTE hs_note_body>", "Activity date <NOTE hs_timestamp>"])
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["manifest_type"], "non_imported_notes_audit_manifest")
        self.assertTrue(manifest["batch_id"])
        self.assertEqual(len(manifest["csv_sha256"]), 64)
        self.assertTrue(all(len(row["row_sha256"]) == 64 for row in manifest["rows"]))
        self.assertEqual([row["csv_row_number"] for row in manifest["rows"]], [2, 3])
        self.assertEqual(
            {tuple(item["source_key"].values()) for item in manifest["rows"]},
            {(1, "101", "11"), (1, "101", "12")},
        )
        with self.assertRaises(FileExistsError):
            generate_batch(self.source, self.ledger, batch_dir)

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
        batch = Path(self.temp.name) / "blocked"
        with self.assertRaises(ValueError):
            generate_batch(self.source, self.ledger, batch)
        decisions = Path(self.temp.name) / "policy.csv"
        decisions.write_text(
            f"email_sha256,decision\n{exception['email_sha256']},approve_import\n"
        )
        with self.assertRaises(ValueError):
            approve_shared_emails(self.ledger, decisions, False)
        self.assertEqual(approve_shared_emails(self.ledger, decisions, True), 2)
        generate_batch(self.source, self.ledger, batch, "synthetic-batch")
        with (batch / "notes.csv").open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(
                {row["Email <CONTACT email>"] for row in csv.DictReader(handle)},
                {"synthetic@example.test"},
            )

    def test_batch_state_requires_order_and_import_id(self):
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        batch = Path(self.temp.name) / "batch"
        generate_batch(self.source, self.ledger, batch, "batch-1")
        with self.assertRaises(ValueError):
            record_batch_state(self.ledger, "batch-1", "submitted", "import-1")
        record_batch_state(self.ledger, "batch-1", "reviewed", reviewer="Synthetic Reviewer")
        with self.assertRaises(ValueError):
            record_batch_state(self.ledger, "batch-1", "submitted")
        with self.assertRaises(ValueError):
            record_batch_state(self.ledger, "batch-1", "submitted", "import-1")
        record_batch_state(self.ledger, "batch-1", "submitted", "import-1",
                           operator="Synthetic Operator")
        ledger = open_ledger(self.ledger)
        self.assertEqual(ledger.execute(
            "SELECT status FROM batches WHERE batch_id='batch-1'"
        ).fetchone()[0], "submitted")
        self.assertEqual({row[0] for row in ledger.execute(
            "SELECT state FROM batch_rows WHERE batch_id='batch-1'"
        )}, {"submitted"})
        ledger.close()

    def test_manual_import_confirms_only_when_totals_and_error_rows_reconcile(self):
        source = sqlite3.connect(self.source)
        source.execute("INSERT INTO JobAdderContacts VALUES (12, 'second@example.test', 1)")
        source.execute("""INSERT INTO JobAdderNotes
                       (Id,noteId,type,source,text,createdAt,JobAdderOrganisationId)
                       VALUES (102,1002,'Phone Call','User','synthetic',3000,1)""")
        source.execute("INSERT INTO JobAdderNoteContacts VALUES (1002, 12, 1)")
        source.commit()
        source.close()
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        batch = Path(self.temp.name) / "batch"
        generate_batch(self.source, self.ledger, batch, "batch-1")
        record_batch_state(self.ledger, "batch-1", "reviewed", reviewer="Reviewer")
        record_batch_state(self.ledger, "batch-1", "submitted", import_name="manual-1",
                           operator="Operator")
        errors = Path(self.temp.name) / "errors.csv"
        errors.write_text("Row Number,Error\n2,Synthetic rejection\n", encoding="utf-8")
        outcome = reconcile_manual_import(
            self.ledger, "batch-1", reported_successful=1, reported_failed=1,
            checked_by="Checker", error_files=[errors],
            evidence_directory=Path(self.temp.name) / "evidence")
        self.assertEqual(outcome, "confirmed_by_import")
        ledger = open_ledger(self.ledger)
        rows = dict(ledger.execute(
            "SELECT csv_row_number, state FROM batch_rows WHERE batch_id='batch-1'"))
        self.assertEqual(rows, {2: "rejected", 3: "confirmed_by_import"})
        self.assertIsNone(ledger.execute(
            "SELECT hubspot_activity_id FROM batch_rows WHERE csv_row_number=3").fetchone()[0])
        evidence = ledger.execute("SELECT * FROM import_error_files").fetchone()
        self.assertEqual(len(evidence["file_sha256"]), 64)
        self.assertTrue(Path(evidence["stored_file"]).exists())
        ledger.close()

    def test_unmatched_manual_import_evidence_requires_reconciliation(self):
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        generate_batch(self.source, self.ledger, Path(self.temp.name) / "batch", "batch-1")
        record_batch_state(self.ledger, "batch-1", "reviewed", reviewer="Reviewer")
        record_batch_state(self.ledger, "batch-1", "submitted", "id-1", operator="Operator")
        outcome = reconcile_manual_import(
            self.ledger, "batch-1", reported_successful=2, reported_failed=1,
            checked_by="Checker", error_files=[],
            evidence_directory=Path(self.temp.name) / "evidence")
        self.assertEqual(outcome, "reconciliation_required")
        ledger = open_ledger(self.ledger)
        self.assertEqual({r[0] for r in ledger.execute("SELECT state FROM batch_rows")},
                         {"reconciliation_required"})
        ledger.close()

    def test_stronger_confirmation_records_sanitized_scope(self):
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        generate_batch(self.source, self.ledger, Path(self.temp.name) / "batch", "batch-1")
        selection = {"contact_reference": "sha256:synthetic", "date_from": "1970-01-01",
                     "date_to": "1970-01-02", "activity_types": ["CALL"]}
        record_stronger_confirmation(
            self.ledger, "batch-1", "confirmed_by_export_sample",
            checked_by="Checker", selection=selection,
            observation="One synthetic row matched expected timestamp and type")
        ledger = open_ledger(self.ledger)
        check = ledger.execute("SELECT * FROM confirmation_checks").fetchone()
        self.assertEqual(check["evidence_state"], "confirmed_by_export_sample")
        self.assertEqual(json.loads(check["selection_json"]), selection)
        ledger.close()

    def test_selection_and_manifest_metadata_are_auditable(self):
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        directory = Path(self.temp.name) / "selected-batch"
        counts = generate_batch(
            self.source, self.ledger, directory, "selection-1",
            environment="test", target_portal_label="synthetic-sandbox",
            selection={
                "contact_ids": ["11"], "emails": ["SYNTHETIC@example.test"],
                "classifications": ["CALL"], "source_types": ["User"],
                "date_from": "1970-01-01T00:00:02Z", "date_from_inclusive": True,
                "date_to": "1970-01-01T00:00:02Z", "date_to_inclusive": True,
            }, operator_notes="synthetic fixture",
        )
        self.assertEqual(counts["row_count"], 1)
        self.assertEqual(counts["unique_source_activities"], 1)
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["environment"], "test")
        self.assertEqual(manifest["target_portal_label"], "synthetic-sandbox")
        self.assertEqual(manifest["row_count"], 1)
        self.assertEqual(len(manifest["mapping_hash"]), 64)
        self.assertEqual(len(manifest["source_data_fingerprint"]), 64)
        self.assertEqual(manifest["generated_file_hash"], manifest["csv_sha256"])
        self.assertEqual(manifest["selection_filters"]["email_sha256"],
                         [email_reference("synthetic@example.test")])
        self.assertEqual((directory / "notes.csv").stat().st_mode & 0o222, 0)
        with self.assertRaises(ValueError):
            generate_batch(self.source, self.ledger,
                           Path(self.temp.name) / "different-dir", "selection-1")

    def test_render_as_notes_preserves_mapping_but_outputs_notes(self):
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        directory = Path(self.temp.name) / "all-as-notes"
        counts = generate_batch(
            self.source, self.ledger, directory, "all-as-notes-1",
            environment="sandbox", target_portal_label="synthetic-sandbox",
            render_as_notes=True,
        )
        self.assertEqual(counts["row_count"], 1)
        self.assertEqual(counts["unique_source_activities"], 1)
        with (directory / "notes.csv").open(
                encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["Email <CONTACT email>"], "synthetic@example.test")
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["selection_filters"]["output_activity_type"], "NOTE")
        ledger = open_ledger(self.ledger)
        self.assertEqual(
            ledger.execute("SELECT mapping_decision FROM activity_links").fetchone()[0],
            "CALL",
        )
        ledger.close()

    def _sandbox_collapsed_batch(self, name="collapsed"):
        directory = Path(self.temp.name) / name
        counts = generate_batch(
            self.source, self.ledger, directory, name,
            environment="sandbox", target_portal_label="synthetic-sandbox",
            render_as_notes=True, sandbox_collapse_by_email=True,
        )
        with (directory / "notes.csv").open(
                encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return directory, counts, rows

    def test_sandbox_collapses_shared_email_and_audits_every_source_key(self):
        source = sqlite3.connect(self.source)
        source.execute("INSERT INTO JobAdderContacts VALUES (12, ' SYNTHETIC@EXAMPLE.TEST ', 1)")
        source.execute("INSERT INTO JobAdderNoteContacts VALUES (1001, 12, 1)")
        source.commit(); source.close()
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)

        directory, counts, rows = self._sandbox_collapsed_batch()
        self.assertEqual(len(rows), 1)
        self.assertEqual(counts["collapsed_source_rows"], 1)
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["sandbox_policy"], "sandbox-collapse-by-email-v1")
        self.assertEqual(
            {key["source_contact_id"] for key in manifest["rows"][0]["contributing_source_keys"]},
            {"11", "12"},
        )
        ledger = open_ledger(self.ledger)
        batch = ledger.execute("SELECT * FROM batches WHERE batch_id='collapsed'").fetchone()
        self.assertEqual((batch["sandbox_policy"], batch["collapsed_source_row_count"]),
                         ("sandbox-collapse-by-email-v1", 1))
        self.assertEqual(ledger.execute("SELECT COUNT(*) FROM batch_row_sources").fetchone()[0], 2)
        ledger.close()

    def test_sandbox_copied_activity_keeps_distinct_recipient_emails(self):
        source = sqlite3.connect(self.source)
        source.execute("INSERT INTO JobAdderContacts VALUES (12, 'second@example.test', 1)")
        source.execute("INSERT INTO JobAdderNoteContacts VALUES (1001, 12, 1)")
        source.commit(); source.close()
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        _, counts, rows = self._sandbox_collapsed_batch()
        self.assertEqual(counts["row_count"], 2)
        self.assertEqual({row["Email <CONTACT email>"] for row in rows},
                         {"synthetic@example.test", "second@example.test"})

    def test_sandbox_keeps_distinct_activities_with_identical_bodies(self):
        source = sqlite3.connect(self.source)
        source.execute("""INSERT INTO JobAdderNotes
                       (Id,noteId,type,source,text,createdAt,JobAdderOrganisationId)
                       VALUES (102,1002,'Phone Call','User','synthetic',2000,1)""")
        source.execute("INSERT INTO JobAdderNoteContacts VALUES (1002,11,1)")
        source.commit(); source.close()
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        _, counts, rows = self._sandbox_collapsed_batch()
        self.assertEqual(counts["row_count"], 2)
        self.assertEqual(len(rows), 2)

    def test_sandbox_excludes_all_contributors_after_submission(self):
        source = sqlite3.connect(self.source)
        source.execute("INSERT INTO JobAdderContacts VALUES (12, 'synthetic@example.test', 1)")
        source.execute("INSERT INTO JobAdderNoteContacts VALUES (1001, 12, 1)")
        source.commit(); source.close()
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        self._sandbox_collapsed_batch("first")
        record_batch_state(self.ledger, "first", "reviewed", reviewer="Reviewer")
        record_batch_state(self.ledger, "first", "submitted", import_id="sandbox-import",
                           operator="Operator")
        second = Path(self.temp.name) / "second"
        counts = generate_batch(
            self.source, self.ledger, second, "second",
            environment="sandbox", target_portal_label="synthetic-sandbox",
            render_as_notes=True, sandbox_collapse_by_email=True,
        )
        self.assertEqual(counts["row_count"], 0)
        self.assertEqual(counts["importable_pairs_before_prior_batches"], 2)
        self.assertEqual(counts["previously_batched_pairs_excluded"], 2)
        self.assertEqual(counts["remaining_importable_pairs"], 0)
        self.assertFalse(second.exists())

    def test_batch_preflight_excludes_prior_rows_in_one_ledger_query(self):
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        self._sandbox_collapsed_batch("first")
        ledger = open_ledger(self.ledger)
        statements = []
        ledger.set_trace_callback(statements.append)
        remaining = list(importable_links(
            ledger, exclude_previously_batched=True))
        ledger.close()
        self.assertEqual(remaining, [])
        selects = [statement for statement in statements
                   if statement.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1)
        self.assertIn("NOT EXISTS", selects[0])

    def test_sandbox_collapse_rejected_outside_sandbox_or_without_notes(self):
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        with self.assertRaisesRegex(ValueError, "environment sandbox"):
            generate_batch(self.source, self.ledger, Path(self.temp.name) / "production",
                           environment="production", render_as_notes=True,
                           sandbox_collapse_by_email=True)
        with self.assertRaisesRegex(ValueError, "render-as-notes"):
            generate_batch(self.source, self.ledger, Path(self.temp.name) / "not-notes",
                           environment="sandbox", sandbox_collapse_by_email=True)

    def test_controlled_supersession_preserves_history_and_blocks_current_version(self):
        discover(self.source, self.ledger, self.mapping, "2026-01-01T00:00:00Z", 1)
        first = Path(self.temp.name) / "policy-v1"
        generate_batch(self.source, self.ledger, first, "policy-v1")
        db = open_ledger(self.ledger)
        with db:
            db.execute("UPDATE batches SET body_transformation_version='note-body-v1' WHERE batch_id='policy-v1'")
            db.execute("UPDATE batch_rows SET body_transformation_version='note-body-v1' WHERE batch_id='policy-v1'")
        old_processing = db.execute("SELECT COUNT(*) FROM note_processing").fetchone()[0]
        db.close()
        second = Path(self.temp.name) / "policy-v2"
        counts = generate_batch(self.source, self.ledger, second, "policy-v2",
            regenerate=True, supersedes_batch_id="policy-v1",
            regeneration_reason="synthetic approved transformation change")
        self.assertEqual(counts["row_count"], 1)
        db = open_ledger(self.ledger)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM batch_rows").fetchone()[0], 2)
        row = db.execute("SELECT * FROM batch_rows WHERE batch_id='policy-v2'").fetchone()
        self.assertEqual((row["supersedes_batch_id"], row["prior_csv_row_number"]),
                         ("policy-v1", 2))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM note_processing").fetchone()[0],
                         old_processing)
        db.close()
        third = Path(self.temp.name) / "policy-v2-repeat"
        repeat = generate_batch(self.source, self.ledger, third, "policy-v2-repeat",
            regenerate=True, supersedes_batch_id="policy-v1",
            regeneration_reason="synthetic repeat")
        self.assertEqual(repeat["row_count"], 0)
        self.assertFalse(third.exists())

    def test_supersession_refuses_unsafe_prior_import_states(self):
        for state in ("submitted", "confirmed_by_import", "reconciliation_required", "reviewed"):
            with self.subTest(state=state):
                name = f"unsafe-{state}"
                db = open_ledger(self.ledger)
                with db:
                    db.execute("""INSERT INTO batches
                        (batch_id,csv_file,csv_sha256,manifest_file,status,created_at_utc,
                         environment,target_portal_label,selection_filters_json,mapping_hash,
                         source_data_fingerprint,row_count,body_transformation_version,import_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (name, "x", hashlib.sha256(name.encode()).hexdigest(), "m", state,
                         "2026-01-01T00:00:00Z", "sandbox", "test", "{}", "x", "x", 0,
                         "note-body-v1", "import-id" if state != "reviewed" else None))
                db.close()
                with self.assertRaisesRegex(ValueError, "prior import state is unsafe"):
                    generate_batch(self.source, self.ledger, Path(self.temp.name) / name,
                        regenerate=True, supersedes_batch_id=name,
                        regeneration_reason="must be refused")


if __name__ == "__main__":
    unittest.main()
