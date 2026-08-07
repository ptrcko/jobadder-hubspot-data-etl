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
import sqlite3
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


NO_EMAIL_REASON = "source_contact_has_no_email_and_is_not_in_hubspot"
IMPORTABLE_STATUSES = {"approved_contact_match"}
IMPORTABLE_MAPPING_DECISIONS = {"CALL", "OUTBOUND_EMAIL", "INBOUND_EMAIL", "NOTE"}
CSV_FIELDS = ["Contact email", "Activity type", "Activity timestamp", "Activity body"]


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
                   c.contactId AS existing_source_contact,
                   CASE WHEN c.email IS NOT NULL AND TRIM(c.email) <> ''
                        THEN 1 ELSE 0 END AS has_email
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
                no_email = not row["has_email"]
                status = "unmatched_contact" if no_email else "pending_contact_resolution"
                reason = (
                    "source_contact_missing"
                    if missing_contact
                    else NO_EMAIL_REASON if no_email else None
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
                if cursor.rowcount:
                    ledger.execute(
                        """INSERT INTO state_transitions
                        (organisation_id, source_contact_id, source_activity_id,
                         from_status, to_status, reason_code, occurred_at_utc)
                        VALUES (?, ?, ?, NULL, ?, ?, ?)""",
                        (organisation_id, str(row["source_contact_id"]),
                         str(row["source_activity_id"]), status, reason, now),
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
        WHERE reason_code = ?
        GROUP BY activity_type, status ORDER BY activity_type, status
        """, (NO_EMAIL_REASON,)
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
               SUM(CASE WHEN status IN ('approved_contact_match')
                         AND mapping_decision IN ('CALL', 'OUTBOUND_EMAIL',
                                                  'INBOUND_EMAIL', 'NOTE')
                         AND hubspot_contact_id IS NOT NULL
                        THEN 1 ELSE 0 END) AS expected_hubspot_activity_creations
        FROM activity_links
        """
    ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


def importable_links(connection: sqlite3.Connection):
    """Yield only links explicitly approved for a pre-existing HubSpot contact.

    CSV generators must use this boundary rather than selecting ledger rows
    directly.  Unmatched contacts can therefore never leak into an import file.
    """
    status_placeholders = ",".join("?" for _ in IMPORTABLE_STATUSES)
    mapping_placeholders = ",".join("?" for _ in IMPORTABLE_MAPPING_DECISIONS)
    return connection.execute(
        f"""SELECT * FROM activity_links
        WHERE status IN ({status_placeholders})
          AND mapping_decision IN ({mapping_placeholders})
          AND hubspot_contact_id IS NOT NULL
        ORDER BY source_activity_id, source_contact_id""",
        (*sorted(IMPORTABLE_STATUSES), *sorted(IMPORTABLE_MAPPING_DECISIONS)),
    )


def write_report(ledger_path: Path, output: Path, report_kind: str) -> None:
    connection = open_ledger(ledger_path)
    try:
        payload = {
            "report": report_kind,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "counts": migration_counts(connection),
            "unmatched_contacts_without_source_email": unmatched_summary(connection),
        }
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    finally:
        connection.close()


def generate_batch(
    source_path: Path, ledger_path: Path, csv_path: Path, manifest_path: Path
) -> dict[str, int]:
    """Create a reviewed candidate CSV and its non-imported row audit manifest.

    This is generation only: it performs no HubSpot write. Existing outputs are
    refused so a reviewed batch cannot be silently rewritten.
    """
    if csv_path.exists() or manifest_path.exists():
        raise FileExistsError("batch CSV and manifest outputs must be new files")
    source = open_read_only(source_path)
    ledger = open_ledger(ledger_path)
    try:
        validate_database(source)
        links = list(importable_links(ledger))
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
                if source_row is None or not (source_row["email"] or "").strip():
                    raise ValueError("eligible pair has no source contact email")
                writer.writerow({
                    "Contact email": source_row["email"].strip(),
                    "Activity type": link["mapping_decision"],
                    "Activity timestamp": link["activity_timestamp"],
                    "Activity body": source_row["text"] or "",
                })
                mapping_hashes.add(link["mapping_fingerprint"])
                audit_rows.append({
                    "csv_row_number": csv_row_number,
                    "source_key": {
                        "organisation_id": link["organisation_id"],
                        "source_activity_id": link["source_activity_id"],
                        "source_contact_id": link["source_contact_id"],
                    },
                })
        csv_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        counts = migration_counts(ledger)
        manifest = {
            "manifest_type": "non_imported_audit_manifest",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "csv_file": csv_path.name,
            "csv_sha256": csv_hash,
            "mapping_fingerprints": sorted(mapping_hashes),
            "counts": counts,
            "rows": audit_rows,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return counts
    except Exception:
        # A failed generation is not a reviewable immutable batch.
        csv_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise
    finally:
        source.close()
        ledger.close()


def reconsider(ledger_path: Path, matches_path: Path, confirmed: bool) -> int:
    if not confirmed:
        raise ValueError(
            "reconsideration requires --confirm-contacts-already-exist-in-hubspot"
        )
    connection = open_ledger(ledger_path)
    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    try:
        with matches_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"source_contact_id", "hubspot_contact_id"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError("approved matches require source_contact_id,hubspot_contact_id")
            with connection:
                for match in reader:
                    source_id = (match["source_contact_id"] or "").strip()
                    hubspot_id = (match["hubspot_contact_id"] or "").strip()
                    if not source_id or not hubspot_id:
                        raise ValueError("approved match IDs must not be blank")
                    affected = list(connection.execute(
                        """SELECT organisation_id, source_activity_id, status
                        FROM activity_links WHERE source_contact_id = ?
                          AND status = 'unmatched_contact' AND reason_code = ?""",
                        (source_id, NO_EMAIL_REASON),
                    ))
                    for row in affected:
                        connection.execute(
                            """UPDATE activity_links SET status = 'approved_contact_match',
                            hubspot_contact_id = ?, reconsidered_at_utc = ?
                            WHERE organisation_id = ? AND source_contact_id = ?
                              AND source_activity_id = ?""",
                            (hubspot_id, now, row["organisation_id"], source_id,
                             row["source_activity_id"]),
                        )
                        connection.execute(
                            """INSERT INTO state_transitions
                            (organisation_id, source_contact_id, source_activity_id,
                             from_status, to_status, reason_code, occurred_at_utc)
                            VALUES (?, ?, ?, ?, 'approved_contact_match',
                            'operator_confirmed_contact_created_separately', ?)""",
                            (row["organisation_id"], source_id,
                             row["source_activity_id"], row["status"], now),
                        )
                        updated += 1
        return updated
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
    retry = commands.add_parser("reconsider-unmatched")
    retry.add_argument("ledger", type=Path)
    retry.add_argument("approved_matches", type=Path)
    retry.add_argument("--confirm-contacts-already-exist-in-hubspot", action="store_true")
    batch = commands.add_parser("generate-batch")
    batch.add_argument("database", type=Path)
    batch.add_argument("ledger", type=Path)
    batch.add_argument("csv", type=Path)
    batch.add_argument("manifest", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "discover":
        print(json.dumps(discover(args.database, args.ledger, args.mapping, args.cutoff, args.organisation_id)))
    elif args.command in {"preview", "reconcile"}:
        write_report(args.ledger, args.output, args.command)
    elif args.command == "reconsider-unmatched":
        print(f"Reconsidered links: {reconsider(args.ledger, args.approved_matches, args.confirm_contacts_already_exist_in_hubspot)}")
    else:
        print(json.dumps(generate_batch(args.database, args.ledger, args.csv, args.manifest)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
