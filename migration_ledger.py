#!/usr/bin/env python3
"""Restartable discovery ledger for the future JobAdder activity migration.

This utility never writes to JobAdder or HubSpot.  In particular, reconsidering
an unmatched contact only records an operator-supplied, pre-existing HubSpot
record ID; it does not create a contact.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from hubspot_history_audit import (
    classify,
    cutoff_milliseconds,
    display_timestamp,
    load_mapping,
    open_read_only,
    validate_cutoff,
    validate_database,
)


NO_EMAIL_REASON = "source_contact_has_no_email"
INVALID_EMAIL_REASON = "source_contact_has_invalid_email"
SHARED_EMAIL_REASON = "multiple_source_contacts_share_normalized_email"
IMPORTABLE_STATUSES = {"approved_email_match", "shared_email_policy_approved"}
IMPORTABLE_MAPPING_DECISIONS = {"CALL", "OUTBOUND_EMAIL", "INBOUND_EMAIL", "NOTE"}
CSV_FIELDS = ["Contact email", "Activity type", "Activity timestamp", "Activity body"]


def file_fingerprint(path: Path) -> str:
    """Hash source bytes without ever opening the source database writable."""
    digest = hashlib.sha256()
    with path.expanduser().resolve().open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_email(value: str | None) -> str | None:
    """Return the exact-association form, or None for blank/invalid input."""
    candidate = (value or "").strip().casefold()
    if not candidate or len(candidate) > 254 or candidate.count("@") != 1:
        return None
    local, domain = candidate.rsplit("@", 1)
    if (not local or len(local) > 64 or not domain or "." not in domain
            or any(char.isspace() for char in candidate)
            or local.startswith(".") or local.endswith(".") or ".." in local):
        return None
    labels = domain.split(".")
    if any(not label or len(label) > 63 or label.startswith("-")
           or label.endswith("-") or not re.fullmatch(r"[a-z0-9-]+", label)
           for label in labels):
        return None
    return candidate


def email_reference(normalized_email: str) -> str:
    """Privacy-safe stable reference used in ledgers and exception reports."""
    return hashlib.sha256(normalized_email.encode("utf-8")).hexdigest()


def mapping_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.expanduser().resolve().read_bytes()).hexdigest()


def open_ledger(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.expanduser().resolve())
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS activity_links (
            organisation_id INTEGER NOT NULL,
            source_contact_id TEXT NOT NULL,
            source_activity_id TEXT NOT NULL,
            source_note_id TEXT,
            activity_type TEXT NOT NULL,
            activity_source TEXT NOT NULL,
            activity_timestamp TEXT NOT NULL,
            mapping_decision TEXT NOT NULL,
            mapping_reason TEXT NOT NULL,
            mapping_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            reason_code TEXT,
            hubspot_contact_id TEXT,
            discovered_at_utc TEXT NOT NULL,
            reconsidered_at_utc TEXT,
            PRIMARY KEY (organisation_id, source_activity_id, source_contact_id)
        );
        CREATE TABLE IF NOT EXISTS state_transitions (
            id INTEGER PRIMARY KEY,
            organisation_id INTEGER NOT NULL,
            source_contact_id TEXT NOT NULL,
            source_activity_id TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            reason_code TEXT,
            occurred_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contact_email_plans (
            organisation_id INTEGER NOT NULL,
            source_contact_id TEXT NOT NULL,
            email_sha256 TEXT,
            status TEXT NOT NULL,
            reason_code TEXT,
            planned_at_utc TEXT NOT NULL,
            policy_decided_at_utc TEXT,
            PRIMARY KEY (organisation_id, source_contact_id)
        );
        CREATE TABLE IF NOT EXISTS batches (
            batch_id TEXT PRIMARY KEY,
            csv_file TEXT NOT NULL,
            csv_sha256 TEXT NOT NULL UNIQUE,
            manifest_file TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('planned', 'reviewed', 'submitted', 'imported', 'rejected')),
            import_id TEXT,
            created_at_utc TEXT NOT NULL,
            environment TEXT NOT NULL,
            target_portal_label TEXT NOT NULL,
            selection_filters_json TEXT NOT NULL,
            mapping_hash TEXT NOT NULL,
            source_data_fingerprint TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            reviewer TEXT,
            review_status TEXT NOT NULL DEFAULT 'pending',
            hubspot_import_name TEXT,
            import_started_at_utc TEXT,
            import_completed_at_utc TEXT,
            result_counts_json TEXT,
            operator_notes TEXT
        );
        CREATE TABLE IF NOT EXISTS batch_rows (
            batch_id TEXT NOT NULL REFERENCES batches(batch_id),
            csv_row_number INTEGER NOT NULL,
            organisation_id INTEGER NOT NULL,
            source_activity_id TEXT NOT NULL,
            source_contact_id TEXT NOT NULL,
            row_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN
                ('planned', 'submitted', 'imported', 'rejected')),
            PRIMARY KEY (batch_id, csv_row_number),
            UNIQUE (organisation_id, source_activity_id, source_contact_id)
        );
        """
    )
    # Upgrade ledgers created by earlier versions without rewriting history.
    existing = {row[1] for row in connection.execute("PRAGMA table_info(batches)")}
    additions = {
        "environment": "TEXT NOT NULL DEFAULT 'unspecified'",
        "target_portal_label": "TEXT NOT NULL DEFAULT 'unspecified'",
        "selection_filters_json": "TEXT NOT NULL DEFAULT '{}'",
        "mapping_hash": "TEXT NOT NULL DEFAULT ''",
        "source_data_fingerprint": "TEXT NOT NULL DEFAULT ''",
        "row_count": "INTEGER NOT NULL DEFAULT 0",
        "reviewer": "TEXT", "review_status": "TEXT NOT NULL DEFAULT 'pending'",
        "hubspot_import_name": "TEXT", "import_started_at_utc": "TEXT",
        "import_completed_at_utc": "TEXT", "result_counts_json": "TEXT",
        "operator_notes": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE batches ADD COLUMN {name} {declaration}")
    return connection


def discover(
    source_path: Path,
    ledger_path: Path,
    mapping_path: Path,
    cutoff: str,
    organisation_id: int,
) -> dict[str, int]:
    """Record every source activity/contact link, including contacts without email."""
    cutoff = validate_cutoff(cutoff)
    cutoff_ms = cutoff_milliseconds(cutoff)
    mapping = load_mapping(mapping_path)
    fingerprint = mapping_fingerprint(mapping_path)
    source = open_read_only(source_path)
    ledger = open_ledger(ledger_path)
    counts: defaultdict[str, int] = defaultdict(int)
    try:
        validate_database(source)
        rows = source.execute(
            """
            SELECT nc.contactId AS source_contact_id,
                   n.Id AS source_activity_id, n.noteId AS source_note_id,
                   COALESCE(n.type, '') AS activity_type,
                   COALESCE(n.source, '') AS activity_source,
                   n.createdAt AS activity_timestamp,
                   c.contactId AS existing_source_contact, c.email AS source_email
            FROM JobAdderNotes n
            JOIN JobAdderNoteContacts nc
              ON nc.JobAdderOrganisationId = n.JobAdderOrganisationId
             AND nc.noteId = n.noteId
            LEFT JOIN JobAdderContacts c
              ON c.JobAdderOrganisationId = nc.JobAdderOrganisationId
             AND c.contactId = nc.contactId
            WHERE n.JobAdderOrganisationId = ?
              AND (((TYPEOF(n.createdAt) IN ('integer', 'real')) AND n.createdAt < ?)
                OR ((TYPEOF(n.createdAt) NOT IN ('integer', 'real')) AND n.createdAt < ?))
            ORDER BY n.Id, nc.contactId
            """,
            (organisation_id, cutoff_ms, cutoff),
        )
        now = datetime.now(timezone.utc).isoformat()
        with ledger:
            for row in rows:
                decision = classify(mapping, row["activity_type"], row["activity_source"])
                missing_contact = row["existing_source_contact"] is None
                raw_email = row["source_email"]
                normalized_email = normalize_email(raw_email)
                no_email = not (raw_email or "").strip()
                status = "approved_email_match" if normalized_email else "unmatched_contact"
                reason = (
                    "source_contact_missing"
                    if missing_contact
                    else NO_EMAIL_REASON if no_email
                    else INVALID_EMAIL_REASON if normalized_email is None else None
                )
                ledger.execute(
                    """INSERT INTO contact_email_plans
                    (organisation_id, source_contact_id, email_sha256, status,
                     reason_code, planned_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(organisation_id, source_contact_id) DO UPDATE SET
                      email_sha256=excluded.email_sha256, status=excluded.status,
                      reason_code=excluded.reason_code, planned_at_utc=excluded.planned_at_utc
                    WHERE contact_email_plans.status NOT IN
                      ('shared_email_policy_approved', 'manually_excluded')""",
                    (organisation_id, str(row["source_contact_id"]),
                     email_reference(normalized_email) if normalized_email else None,
                     status, reason, now),
                )
                values = (
                    organisation_id, str(row["source_contact_id"]),
                    str(row["source_activity_id"]), str(row["source_note_id"]),
                    row["activity_type"], row["activity_source"],
                    display_timestamp(row["activity_timestamp"]),
                    decision["classification"], decision["reason"], fingerprint,
                    status, reason, now,
                )
                cursor = ledger.execute(
                    """
                    INSERT OR IGNORE INTO activity_links
                    (organisation_id, source_contact_id, source_activity_id,
                     source_note_id, activity_type, activity_source,
                     activity_timestamp, mapping_decision, mapping_reason,
                     mapping_fingerprint, status, reason_code, discovered_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                counts["inserted" if cursor.rowcount else "already_recorded"] += 1
                if not cursor.rowcount:
                    ledger.execute(
                        """UPDATE activity_links SET status=?, reason_code=?,
                           mapping_decision=?, mapping_reason=?, mapping_fingerprint=?
                           WHERE organisation_id=? AND source_contact_id=?
                           AND source_activity_id=?
                           AND status NOT IN ('shared_email_policy_approved',
                                              'manually_excluded', 'submitted',
                                              'confirmed', 'rejected')""",
                        (status, reason, decision["classification"], decision["reason"],
                         fingerprint, organisation_id,
                         str(row["source_contact_id"]),
                         str(row["source_activity_id"])),
                    )
                if cursor.rowcount:
                    ledger.execute(
                        """INSERT INTO state_transitions
                        (organisation_id, source_contact_id, source_activity_id,
                         from_status, to_status, reason_code, occurred_at_utc)
                        VALUES (?, ?, ?, NULL, ?, ?, ?)""",
                        (organisation_id, str(row["source_contact_id"]),
                         str(row["source_activity_id"]), status, reason, now),
                    )
            shared = list(ledger.execute(
                """SELECT email_sha256 FROM contact_email_plans
                WHERE organisation_id = ? AND email_sha256 IS NOT NULL
                GROUP BY email_sha256 HAVING COUNT(DISTINCT source_contact_id) > 1""",
                (organisation_id,),
            ))
            for item in shared:
                ledger.execute(
                    """UPDATE contact_email_plans
                    SET status='shared_email_exception', reason_code=?
                    WHERE organisation_id=? AND email_sha256=?
                      AND status <> 'shared_email_policy_approved'""",
                    (SHARED_EMAIL_REASON, organisation_id, item["email_sha256"]),
                )
                ledger.execute(
                    """UPDATE activity_links SET status='shared_email_exception',
                       reason_code=? WHERE organisation_id=? AND source_contact_id IN
                       (SELECT source_contact_id FROM contact_email_plans
                        WHERE organisation_id=? AND email_sha256=?)
                       AND status <> 'shared_email_policy_approved'""",
                    (SHARED_EMAIL_REASON, organisation_id, organisation_id,
                     item["email_sha256"]),
                )
        return dict(counts)
    finally:
        source.close()
        ledger.close()


def unmatched_summary(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Aggregate safe-to-share counts for previews and reconciliation reports."""
    return [dict(row) for row in connection.execute(
        """
        SELECT activity_type, status, COUNT(*) AS record_count,
               MIN(activity_timestamp) AS earliest_activity,
               MAX(activity_timestamp) AS latest_activity
        FROM activity_links
        WHERE status = 'unmatched_contact'
        GROUP BY activity_type, status ORDER BY activity_type, status
        """
    )]


def migration_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Return activity and association counts without conflating the two."""
    row = connection.execute(
        """
        SELECT (SELECT COUNT(*) FROM (
                   SELECT organisation_id, source_activity_id
                   FROM activity_links GROUP BY organisation_id, source_activity_id
               )) AS unique_source_activities,
               COUNT(*) AS activity_contact_pairs,
               SUM(CASE WHEN status IN ('approved_email_match',
                                         'shared_email_policy_approved')
                         AND mapping_decision IN ('CALL', 'OUTBOUND_EMAIL',
                                                  'INBOUND_EMAIL', 'NOTE')
                        THEN 1 ELSE 0 END) AS expected_hubspot_activity_creations
        FROM activity_links
        """
    ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


def importable_links(connection: sqlite3.Connection, selection: dict | None = None):
    """Yield only links approved for normalized exact-email association.

    CSV generators must use this boundary rather than selecting ledger rows
    directly.  Unmatched contacts can therefore never leak into an import file.
    """
    status_placeholders = ",".join("?" for _ in IMPORTABLE_STATUSES)
    mapping_placeholders = ",".join("?" for _ in IMPORTABLE_MAPPING_DECISIONS)
    selection = selection or {}
    clauses, values = [], []
    scalar_filters = {
        "contact_ids": "source_contact_id", "classifications": "mapping_decision",
        "source_types": "activity_source",
    }
    for key, column in scalar_filters.items():
        chosen = selection.get(key) or []
        if chosen:
            clauses.append(f"{column} IN ({','.join('?' for _ in chosen)})")
            values.extend(str(item) for item in chosen)
    if selection.get("date_from"):
        clauses.append("activity_timestamp >= ?" if selection.get("date_from_inclusive", True)
                       else "activity_timestamp > ?")
        values.append(selection["date_from"])
    if selection.get("date_to"):
        clauses.append("activity_timestamp <= ?" if selection.get("date_to_inclusive", True)
                       else "activity_timestamp < ?")
        values.append(selection["date_to"])
    extra = " AND " + " AND ".join(clauses) if clauses else ""
    return connection.execute(
        f"""SELECT * FROM activity_links
        WHERE status IN ({status_placeholders})
          AND mapping_decision IN ({mapping_placeholders})
          {extra}
        ORDER BY source_activity_id, source_contact_id""",
        (*sorted(IMPORTABLE_STATUSES), *sorted(IMPORTABLE_MAPPING_DECISIONS), *values),
    )


def shared_email_exceptions(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Return source IDs and hashes only; never expose contact addresses."""
    return [dict(row) for row in connection.execute(
        """SELECT organisation_id, email_sha256,
                  GROUP_CONCAT(source_contact_id, ',') AS source_contact_ids,
                  COUNT(*) AS source_contact_count
           FROM contact_email_plans WHERE status='shared_email_exception'
           GROUP BY organisation_id, email_sha256 ORDER BY organisation_id, email_sha256"""
    )]


def approve_shared_emails(ledger_path: Path, decisions_path: Path,
                          confirmed: bool) -> int:
    """Apply an explicit reviewed policy decision to a shared-email group."""
    if not confirmed:
        raise ValueError("shared-email rows require --confirm-reviewed-policy")
    connection = open_ledger(ledger_path)
    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    try:
        with decisions_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"email_sha256", "decision"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError("policy file requires email_sha256,decision")
            with connection:
                for row in reader:
                    reference = (row["email_sha256"] or "").strip().lower()
                    decision = (row["decision"] or "").strip().lower()
                    if not re.fullmatch(r"[0-9a-f]{64}", reference):
                        raise ValueError("email_sha256 must be a SHA-256 hex digest")
                    if decision not in {"approve_import", "exclude"}:
                        raise ValueError("decision must be approve_import or exclude")
                    new_status = ("shared_email_policy_approved" if decision == "approve_import"
                                  else "manually_excluded")
                    contacts = list(connection.execute(
                        """SELECT organisation_id, source_contact_id
                           FROM contact_email_plans
                           WHERE email_sha256=? AND status='shared_email_exception'""",
                        (reference,),
                    ))
                    if not contacts:
                        raise ValueError(f"no unresolved shared-email group: {reference}")
                    connection.execute(
                        """UPDATE contact_email_plans SET status=?, reason_code=?,
                           policy_decided_at_utc=? WHERE email_sha256=?
                           AND status='shared_email_exception'""",
                        (new_status, f"explicit_policy_{decision}", now, reference),
                    )
                    for contact in contacts:
                        cursor = connection.execute(
                            """UPDATE activity_links SET status=?, reason_code=?
                               WHERE organisation_id=? AND source_contact_id=?
                               AND status='shared_email_exception'""",
                            (new_status, f"explicit_policy_{decision}",
                             contact["organisation_id"], contact["source_contact_id"]),
                        )
                        updated += cursor.rowcount
        return updated
    finally:
        connection.close()


def write_report(ledger_path: Path, output: Path, report_kind: str) -> None:
    connection = open_ledger(ledger_path)
    try:
        payload = {
            "report": report_kind,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "counts": migration_counts(connection),
            "unmatched_contact_exceptions": unmatched_summary(connection),
            "shared_email_exceptions": shared_email_exceptions(connection),
        }
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    finally:
        connection.close()


def generate_batch(
    source_path: Path, ledger_path: Path, batch_directory: Path,
    batch_id: str | None = None, *, environment: str = "sandbox",
    target_portal_label: str = "unspecified", selection: dict | None = None,
    operator_notes: str | None = None,
) -> dict[str, int]:
    """Create a reviewed candidate CSV and its non-imported row audit manifest.

    This is generation only: it performs no HubSpot write. Existing outputs are
    refused so a reviewed batch cannot be silently rewritten.
    """
    selection = selection or {}
    batch_id = batch_id or str(uuid.uuid4())
    if batch_directory.exists():
        raise FileExistsError("batch directory already exists; create a new batch")
    source = open_read_only(source_path)
    ledger = open_ledger(ledger_path)
    try:
        validate_database(source)
        if ledger.execute("SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)).fetchone():
            raise ValueError("batch_id has already been used; choose a new batch ID")
        links = list(importable_links(ledger, selection))
        selected_emails = {normalize_email(item) for item in selection.get("emails", [])}
        selected_emails.discard(None)
        if selection.get("emails") and len(selected_emails) != len(selection["emails"]):
            raise ValueError("every selected email must be valid")
        if selected_emails:
            filtered = []
            for link in links:
                email = source.execute(
                    """SELECT email FROM JobAdderContacts WHERE JobAdderOrganisationId=?
                       AND contactId=?""",
                    (link["organisation_id"], link["source_contact_id"]),
                ).fetchone()
                if email and normalize_email(email["email"]) in selected_emails:
                    filtered.append(link)
            links = filtered
        if shared_email_exceptions(ledger):
            raise ValueError("unresolved shared-email exceptions require a policy decision")
        exact_selection = {
            "contact_ids": selection.get("contact_ids", []),
            # Never persist or print contact addresses; hashes unambiguously
            # identify the exact normalized email selectors used for the run.
            "email_sha256": sorted(email_reference(item) for item in selected_emails),
            "classifications": selection.get("classifications", []),
            "source_types": selection.get("source_types", []),
            "date_from": selection.get("date_from"),
            "date_from_inclusive": selection.get("date_from_inclusive", True),
            "date_to": selection.get("date_to"),
            "date_to_inclusive": selection.get("date_to_inclusive", True),
        }
        selected_counts = {
            "row_count": len(links),
            "unique_source_activities": len({
                (row["organisation_id"], row["source_activity_id"]) for row in links
            }),
        }
        # This output deliberately precedes mkdir/open: operators see precisely
        # what will be emitted before any generated artifact exists.
        print(json.dumps({"selection": exact_selection, "counts": selected_counts},
                         sort_keys=True))
        if not links:
            raise ValueError("selection produced no importable rows; no batch was created")
        batch_directory.mkdir(parents=False)
        csv_path = batch_directory / "activities.csv"
        manifest_path = batch_directory / "manifest.json"
        audit_rows = []
        mapping_hashes = set()
        with csv_path.open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for csv_row_number, link in enumerate(links, start=2):
                source_row = source.execute(
                    """SELECT c.email, n.text
                    FROM JobAdderNotes n
                    JOIN JobAdderNoteContacts nc
                      ON nc.JobAdderOrganisationId = n.JobAdderOrganisationId
                     AND nc.noteId = n.noteId
                    JOIN JobAdderContacts c
                      ON c.JobAdderOrganisationId = nc.JobAdderOrganisationId
                     AND c.contactId = nc.contactId
                    WHERE n.JobAdderOrganisationId = ? AND n.Id = ?
                      AND nc.contactId = ?""",
                    (link["organisation_id"], link["source_activity_id"],
                     link["source_contact_id"]),
                ).fetchone()
                normalized_email = normalize_email(source_row["email"] if source_row else None)
                if normalized_email is None:
                    raise ValueError("eligible pair has blank or invalid source contact email")
                planned_hash = ledger.execute(
                    """SELECT email_sha256 FROM contact_email_plans
                       WHERE organisation_id=? AND source_contact_id=?""",
                    (link["organisation_id"], link["source_contact_id"]),
                ).fetchone()
                if planned_hash is None or planned_hash["email_sha256"] != email_reference(normalized_email):
                    raise ValueError("source email changed after planning; rediscover before batching")
                output_row = {
                    "Contact email": normalized_email,
                    "Activity type": link["mapping_decision"],
                    "Activity timestamp": link["activity_timestamp"],
                    "Activity body": source_row["text"] or "",
                }
                writer.writerow(output_row)
                mapping_hashes.add(link["mapping_fingerprint"])
                audit_rows.append({
                    "csv_row_number": csv_row_number,
                    "source_key": {
                        "organisation_id": link["organisation_id"],
                        "source_activity_id": link["source_activity_id"],
                        "source_contact_id": link["source_contact_id"],
                    },
                    "row_sha256": hashlib.sha256(json.dumps(
                        output_row, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")).hexdigest(),
                })
        csv_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        mapping_hash = next(iter(mapping_hashes), "")
        if len(mapping_hashes) > 1:
            raise ValueError("selection spans multiple mapping hashes; rediscover first")
        source_hash = file_fingerprint(source_path)
        manifest = {
            "manifest_type": "non_imported_audit_manifest",
            "batch_id": batch_id,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "csv_file": csv_path.name,
            "csv_sha256": csv_hash,
            "environment": environment,
            "target_portal_label": target_portal_label,
            "selection_filters": exact_selection,
            "mapping_hash": mapping_hash,
            "source_data_fingerprint": source_hash,
            "generated_file_hash": csv_hash,
            "row_count": len(audit_rows),
            "reviewer": None,
            "review_status": "pending",
            "hubspot_import_name_or_id": None,
            "import_started_at_utc": None,
            "import_completed_at_utc": None,
            "result_counts": None,
            "operator_notes": operator_notes,
            "counts": selected_counts,
            "rows": audit_rows,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        with ledger:
            ledger.execute(
                """INSERT INTO batches
                   (batch_id, csv_file, csv_sha256, manifest_file, status, created_at_utc,
                    environment, target_portal_label, selection_filters_json,
                    mapping_hash, source_data_fingerprint, row_count, review_status,
                    operator_notes)
                   VALUES (?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (batch_id, csv_path.name, csv_hash, manifest_path.name,
                 manifest["generated_at_utc"], environment, target_portal_label,
                 json.dumps(exact_selection, sort_keys=True), mapping_hash, source_hash,
                 len(audit_rows), operator_notes),
            )
            for item in audit_rows:
                key = item["source_key"]
                ledger.execute(
                    """INSERT INTO batch_rows
                       (batch_id, csv_row_number, organisation_id, source_activity_id,
                        source_contact_id, row_sha256, state)
                       VALUES (?, ?, ?, ?, ?, ?, 'planned')""",
                    (batch_id, item["csv_row_number"], key["organisation_id"],
                     key["source_activity_id"], key["source_contact_id"],
                     item["row_sha256"]),
                )
        # Generation is append-only: review never needs to edit either artifact.
        csv_path.chmod(0o444)
        manifest_path.chmod(0o444)
        return selected_counts
    except Exception:
        # A failed generation is not a reviewable immutable batch.
        if batch_directory.exists():
            for generated in batch_directory.iterdir():
                generated.unlink()
            batch_directory.rmdir()
        raise
    finally:
        source.close()
        ledger.close()


def record_batch_state(ledger_path: Path, batch_id: str, state: str,
                       import_id: str | None = None, *, reviewer: str | None = None,
                       import_name: str | None = None,
                       result_counts: dict | None = None,
                       operator_notes: str | None = None) -> None:
    """Record review/submission/import outcomes; this performs no HubSpot write."""
    allowed = {
        "planned": {"reviewed", "rejected"},
        "reviewed": {"submitted", "rejected"},
        "submitted": {"imported", "rejected"},
    }
    connection = open_ledger(ledger_path)
    try:
        batch = connection.execute(
            "SELECT status FROM batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise ValueError("unknown batch_id")
        if state not in allowed.get(batch["status"], set()):
            raise ValueError(f"invalid batch transition {batch['status']} -> {state}")
        if state in {"submitted", "imported"} and not (import_id or "").strip():
            raise ValueError("submitted/imported states require an import_id")
        if state == "reviewed" and not (reviewer or "").strip():
            raise ValueError("reviewed state requires a reviewer")
        now = datetime.now(timezone.utc).isoformat()
        with connection:
            connection.execute(
                """UPDATE batches SET status=?, import_id=COALESCE(?, import_id),
                   reviewer=COALESCE(?, reviewer),
                   review_status=CASE WHEN ?='reviewed' THEN 'approved'
                                      WHEN ?='rejected' THEN 'rejected' ELSE review_status END,
                   hubspot_import_name=COALESCE(?, hubspot_import_name),
                   import_started_at_utc=CASE WHEN ?='submitted' THEN ?
                                              ELSE import_started_at_utc END,
                   import_completed_at_utc=CASE WHEN ? IN ('imported','rejected') THEN ?
                                                ELSE import_completed_at_utc END,
                   result_counts_json=COALESCE(?, result_counts_json),
                   operator_notes=COALESCE(?, operator_notes)
                   WHERE batch_id=?""",
                (state, (import_id or "").strip() or None,
                 (reviewer or "").strip() or None, state, state,
                 (import_name or "").strip() or None, state, now, state, now,
                 json.dumps(result_counts, sort_keys=True) if result_counts is not None else None,
                 operator_notes, batch_id),
            )
            row_state = state if state in {"submitted", "imported", "rejected"} else "planned"
            connection.execute(
                "UPDATE batch_rows SET state=? WHERE batch_id=?", (row_state, batch_id)
            )
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    discovery = commands.add_parser("discover")
    discovery.add_argument("database", type=Path)
    discovery.add_argument("ledger", type=Path)
    discovery.add_argument("--mapping", type=Path, default=Path(__file__).with_name("activity-mapping.csv"))
    discovery.add_argument("--cutoff", default="2026-06-23T00:00:00")
    discovery.add_argument("--organisation-id", type=int, default=1)
    for name in ("preview", "reconcile"):
        report = commands.add_parser(name)
        report.add_argument("ledger", type=Path)
        report.add_argument("output", type=Path)
    policy = commands.add_parser("decide-shared-emails")
    policy.add_argument("ledger", type=Path)
    policy.add_argument("decisions", type=Path)
    policy.add_argument("--confirm-reviewed-policy", action="store_true")
    batch = commands.add_parser("generate-batch")
    batch.add_argument("database", type=Path)
    batch.add_argument("ledger", type=Path)
    batch.add_argument("batch_directory", type=Path)
    batch.add_argument("--batch-id")
    batch.add_argument("--environment", required=True)
    batch.add_argument("--target-portal-label", required=True)
    batch.add_argument("--contact-id", action="append", default=[])
    batch.add_argument("--email", action="append", default=[])
    batch.add_argument("--classification", action="append", default=[])
    batch.add_argument("--source-type", action="append", default=[])
    batch.add_argument("--date-from")
    batch.add_argument("--date-from-exclusive", action="store_true")
    batch.add_argument("--date-to")
    batch.add_argument("--date-to-exclusive", action="store_true")
    batch.add_argument("--operator-notes")
    state = commands.add_parser("record-batch-state")
    state.add_argument("ledger", type=Path)
    state.add_argument("batch_id")
    state.add_argument("state", choices=("reviewed", "submitted", "imported", "rejected"))
    state.add_argument("--import-id")
    state.add_argument("--import-name")
    state.add_argument("--reviewer")
    state.add_argument("--result-counts-json", type=json.loads)
    state.add_argument("--operator-notes")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "discover":
        print(json.dumps(discover(args.database, args.ledger, args.mapping, args.cutoff, args.organisation_id)))
    elif args.command in {"preview", "reconcile"}:
        write_report(args.ledger, args.output, args.command)
    elif args.command == "decide-shared-emails":
        print(f"Updated links: {approve_shared_emails(args.ledger, args.decisions, args.confirm_reviewed_policy)}")
    elif args.command == "record-batch-state":
        record_batch_state(args.ledger, args.batch_id, args.state, args.import_id,
                           reviewer=args.reviewer, import_name=args.import_name,
                           result_counts=args.result_counts_json,
                           operator_notes=args.operator_notes)
    else:
        selection = {
            "contact_ids": args.contact_id, "emails": args.email,
            "classifications": args.classification, "source_types": args.source_type,
            "date_from": args.date_from,
            "date_from_inclusive": not args.date_from_exclusive,
            "date_to": args.date_to, "date_to_inclusive": not args.date_to_exclusive,
        }
        print(json.dumps(generate_batch(
            args.database, args.ledger, args.batch_directory, args.batch_id,
            environment=args.environment, target_portal_label=args.target_portal_label,
            selection=selection, operator_notes=args.operator_notes)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
