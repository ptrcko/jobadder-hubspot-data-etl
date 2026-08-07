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
            created_at_utc TEXT NOT NULL
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
                        """UPDATE activity_links SET status=?, reason_code=?
                           WHERE organisation_id=? AND source_contact_id=?
                           AND source_activity_id=?
                           AND status NOT IN ('shared_email_policy_approved',
                                              'manually_excluded', 'submitted',
                                              'confirmed', 'rejected')""",
                        (status, reason, organisation_id,
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


def importable_links(connection: sqlite3.Connection):
    """Yield only links approved for normalized exact-email association.

    CSV generators must use this boundary rather than selecting ledger rows
    directly.  Unmatched contacts can therefore never leak into an import file.
    """
    status_placeholders = ",".join("?" for _ in IMPORTABLE_STATUSES)
    mapping_placeholders = ",".join("?" for _ in IMPORTABLE_MAPPING_DECISIONS)
    return connection.execute(
        f"""SELECT * FROM activity_links
        WHERE status IN ({status_placeholders})
          AND mapping_decision IN ({mapping_placeholders})
        ORDER BY source_activity_id, source_contact_id""",
        (*sorted(IMPORTABLE_STATUSES), *sorted(IMPORTABLE_MAPPING_DECISIONS)),
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
    source_path: Path, ledger_path: Path, csv_path: Path, manifest_path: Path,
    batch_id: str | None = None,
) -> dict[str, int]:
    """Create a reviewed candidate CSV and its non-imported row audit manifest.

    This is generation only: it performs no HubSpot write. Existing outputs are
    refused so a reviewed batch cannot be silently rewritten.
    """
    if csv_path.exists() or manifest_path.exists():
        raise FileExistsError("batch CSV and manifest outputs must be new files")
    source = open_read_only(source_path)
    ledger = open_ledger(ledger_path)
    batch_id = batch_id or str(uuid.uuid4())
    try:
        validate_database(source)
        links = list(importable_links(ledger))
        if shared_email_exceptions(ledger):
            raise ValueError("unresolved shared-email exceptions require a policy decision")
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
        counts = migration_counts(ledger)
        manifest = {
            "manifest_type": "non_imported_audit_manifest",
            "batch_id": batch_id,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "csv_file": csv_path.name,
            "csv_sha256": csv_hash,
            "mapping_fingerprints": sorted(mapping_hashes),
            "counts": counts,
            "rows": audit_rows,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        with ledger:
            ledger.execute(
                """INSERT INTO batches
                   (batch_id, csv_file, csv_sha256, manifest_file, status, created_at_utc)
                   VALUES (?, ?, ?, ?, 'planned', ?)""",
                (batch_id, csv_path.name, csv_hash, manifest_path.name,
                 manifest["generated_at_utc"]),
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
        return counts
    except Exception:
        # A failed generation is not a reviewable immutable batch.
        csv_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise
    finally:
        source.close()
        ledger.close()


def record_batch_state(ledger_path: Path, batch_id: str, state: str,
                       import_id: str | None = None) -> None:
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
        with connection:
            connection.execute(
                "UPDATE batches SET status=?, import_id=COALESCE(?, import_id) WHERE batch_id=?",
                (state, (import_id or "").strip() or None, batch_id),
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
    batch.add_argument("csv", type=Path)
    batch.add_argument("manifest", type=Path)
    batch.add_argument("--batch-id")
    state = commands.add_parser("record-batch-state")
    state.add_argument("ledger", type=Path)
    state.add_argument("batch_id")
    state.add_argument("state", choices=("reviewed", "submitted", "imported", "rejected"))
    state.add_argument("--import-id")
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
        record_batch_state(args.ledger, args.batch_id, args.state, args.import_id)
    else:
        print(json.dumps(generate_batch(args.database, args.ledger, args.csv,
                                        args.manifest, args.batch_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
