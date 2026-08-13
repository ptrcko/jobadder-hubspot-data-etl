#!/usr/bin/env python3
"""Restartable discovery ledger for the future JobAdder activity migration.

This utility never writes to JobAdder or HubSpot.  In particular, reconsidering
an unmatched contact only records an operator-supplied, pre-existing HubSpot
record ID; it does not create a contact.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import re
import shutil
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
CSV_FIELDS = [
    "Email <CONTACT email>",
    "Note body <NOTE hs_note_body>",
    "Activity date <NOTE hs_timestamp>",
]
BODY_TRANSFORMATION_VERSION = "note-body-v1"
QUOTED_HISTORY_VERSION = "quoted-history-v1-window-8"
DUPLICATE_POLICY_VERSION = "note-strict-v1"
NOTES_ONLY_POLICY_VERSION = "notes-only-approved-v1"
QUOTED_HEADER_WINDOW = 8


def body_hash(value: str) -> str:
    """Return the only representation of private body content stored in audits."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_note_body(value: str | None) -> str:
    """Apply the versioned, paragraph-preserving notes body transformation."""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    output: list[str] = []
    blank = True
    for line in text.split("\n"):
        line = line.rstrip()
        # NBSP is not consistently treated as ordinary whitespace by external
        # CSV consumers, so make its blank-line treatment explicit.
        is_blank = not line.replace("\u00a0", " ").strip()
        if is_blank:
            if output and not blank:
                output.append("")
            blank = True
        else:
            output.append(line)
            blank = False
    while output and output[-1] == "":
        output.pop()
    return "\n".join(output)


def trim_quoted_history(value: str | None) -> tuple[str | None, str, str]:
    """Conservatively remove one complete Outlook-style quoted header block.

    A boundary is accepted only when From plus Sent/Date, To and Subject occur
    in the next eight lines. Blank lines and optional Cc/Bcc lines are ignored.
    """
    text = value or ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    boundaries: list[int] = []
    header = re.compile(r"^\s*(from|sent|date|to|subject|cc|bcc)\s*:", re.I)
    for index, line in enumerate(lines):
        if not re.match(r"^\s*from\s*:", line, re.I):
            continue
        names = set()
        coherent = True
        for candidate in lines[index:index + QUOTED_HEADER_WINDOW + 1]:
            if not candidate.strip():
                continue
            match = header.match(candidate)
            if match:
                names.add(match.group(1).casefold())
            elif names:
                # Body text may start inside the window once the required
                # coherent header is complete; it is not part of the header.
                complete = ("from" in names and "to" in names and
                            "subject" in names and bool({"sent", "date"} & names))
                coherent = complete
                break
        if coherent and "from" in names and "to" in names and "subject" in names \
                and ({"sent", "date"} & names):
            boundaries.append(index)
    if not boundaries:
        return text, "not_found", "no_complete_header_block"
    if len(boundaries) != 1:
        return None, "review", "conflicting_quoted_history_boundaries"
    retained = "\n".join(lines[:boundaries[0]])
    if len(normalize_note_body(retained).strip()) < 3:
        return None, "review", "retained_content_empty_or_unreasonably_short"
    return retained, "trimmed", "single_complete_header_block"


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
                ('planned', 'reviewed', 'submitted', 'confirmed_by_import',
                 'reconciliation_required', 'rejected')),
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
                ('planned', 'submitted', 'confirmed_by_import',
                 'confirmed_by_export_sample', 'confirmed_manually',
                 'reconciliation_required', 'rejected')),
            hubspot_activity_id TEXT,
            PRIMARY KEY (batch_id, csv_row_number),
            UNIQUE (organisation_id, source_activity_id, source_contact_id)
        );
        CREATE TABLE IF NOT EXISTS import_reconciliations (
            id INTEGER PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batches(batch_id),
            import_id TEXT,
            import_name TEXT,
            submitted_at_utc TEXT NOT NULL,
            submitted_by TEXT NOT NULL,
            checked_at_utc TEXT NOT NULL,
            checked_by TEXT NOT NULL,
            reported_successful INTEGER NOT NULL,
            reported_failed INTEGER NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN
                ('confirmed_by_import', 'reconciliation_required')),
            observation TEXT
        );
        CREATE TABLE IF NOT EXISTS import_error_files (
            id INTEGER PRIMARY KEY,
            reconciliation_id INTEGER NOT NULL REFERENCES import_reconciliations(id),
            stored_file TEXT NOT NULL,
            file_sha256 TEXT NOT NULL,
            error_row_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS import_error_rows (
            reconciliation_id INTEGER NOT NULL REFERENCES import_reconciliations(id),
            batch_id TEXT NOT NULL,
            csv_row_number INTEGER NOT NULL,
            PRIMARY KEY (reconciliation_id, csv_row_number)
        );
        CREATE TABLE IF NOT EXISTS confirmation_checks (
            id INTEGER PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batches(batch_id),
            evidence_state TEXT NOT NULL CHECK(evidence_state IN
                ('confirmed_by_export_sample', 'confirmed_manually')),
            checked_by TEXT NOT NULL,
            checked_at_utc TEXT NOT NULL,
            selection_json TEXT NOT NULL,
            sanitized_observation TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS note_processing (
            organisation_id INTEGER NOT NULL,
            source_contact_id TEXT NOT NULL,
            source_activity_id TEXT NOT NULL,
            raw_body_sha256 TEXT NOT NULL,
            raw_character_count INTEGER NOT NULL,
            transformed_body_sha256 TEXT,
            transformed_character_count INTEGER,
            body_transformation_version TEXT NOT NULL,
            extraction_rule_version TEXT NOT NULL,
            boundary_outcome TEXT NOT NULL,
            boundary_reason_code TEXT NOT NULL,
            target_activity_type TEXT NOT NULL,
            comparison_key_sha256 TEXT,
            survivor_reference_sha256 TEXT,
            duplicate_policy_version TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason_code TEXT,
            PRIMARY KEY (organisation_id, source_activity_id, source_contact_id)
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
        "submitted_by": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE batches ADD COLUMN {name} {declaration}")
    row_columns = {row[1] for row in connection.execute("PRAGMA table_info(batch_rows)")}
    if "hubspot_activity_id" not in row_columns:
        connection.execute("ALTER TABLE batch_rows ADD COLUMN hubspot_activity_id TEXT")
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
                if normalized_email and decision["classification"] == "REVIEW":
                    status, reason = "review", "mapping_review_required"
                elif normalized_email and decision["classification"] == "EXCLUDE":
                    status, reason = "excluded", "mapping_excluded"
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


def _source_key(link: sqlite3.Row) -> tuple[int, str, str]:
    """Stable ordering used to choose the survivor of a strict duplicate set."""
    return (int(link["organisation_id"]), str(link["source_activity_id"]),
            str(link["source_contact_id"]))


def _safe_source_reference(key: tuple[int, str, str]) -> str:
    return hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()


def generate_batch(
    source_path: Path, ledger_path: Path, batch_directory: Path,
    batch_id: str | None = None, *, environment: str = "sandbox",
    target_portal_label: str = "unspecified", selection: dict | None = None,
    operator_notes: str | None = None,
) -> dict[str, int]:
    """Generate one dry-run-only, immutable notes CSV; never submit an import."""
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
        # A source pair already in an immutable batch cannot silently enter another.
        links = [link for link in links if not ledger.execute(
            """SELECT 1 FROM batch_rows WHERE organisation_id=?
               AND source_activity_id=? AND source_contact_id=?""", _source_key(link)
        ).fetchone()]
        selected_emails = {normalize_email(item) for item in selection.get("emails", [])}
        selected_emails.discard(None)
        if selection.get("emails") and len(selected_emails) != len(selection["emails"]):
            raise ValueError("every selected email must be valid")
        if shared_email_exceptions(ledger):
            raise ValueError("unresolved shared-email exceptions require a policy decision")

        candidates = []
        held_trim = 0
        mapping_hashes = set()
        for link in links:
            row = source.execute(
                """SELECT c.email, n.text FROM JobAdderNotes n
                JOIN JobAdderNoteContacts nc ON nc.JobAdderOrganisationId=n.JobAdderOrganisationId
                  AND nc.noteId=n.noteId
                JOIN JobAdderContacts c ON c.JobAdderOrganisationId=nc.JobAdderOrganisationId
                  AND c.contactId=nc.contactId
                WHERE n.JobAdderOrganisationId=? AND n.Id=? AND nc.contactId=?""",
                _source_key(link),
            ).fetchone()
            email = normalize_email(row["email"] if row else None)
            if email is None:
                continue
            if selected_emails and email not in selected_emails:
                continue
            plan = ledger.execute(
                "SELECT email_sha256 FROM contact_email_plans WHERE organisation_id=? AND source_contact_id=?",
                (link["organisation_id"], link["source_contact_id"]),
            ).fetchone()
            if plan is None or plan["email_sha256"] != email_reference(email):
                raise ValueError("source email changed after planning; rediscover before batching")
            raw = row["text"] or ""
            retained, boundary_outcome, boundary_reason = trim_quoted_history(raw)
            normalized = normalize_note_body(retained) if retained is not None else None
            outcome, reason = "eligible", None
            if retained is None or not normalized:
                outcome, reason = "review", boundary_reason if retained is None else "empty_note_body"
                held_trim += boundary_outcome == "review"
            contact_ref = plan["email_sha256"]
            comparison = None if normalized is None else hashlib.sha256(
                f"{contact_ref}|NOTE|{link['activity_timestamp']}|{body_hash(normalized)}".encode()
            ).hexdigest()
            item = {"link": link, "email": email, "body": normalized,
                    "contact_ref": contact_ref, "comparison": comparison,
                    "raw_hash": body_hash(raw), "raw_count": len(raw),
                    "body_hash": body_hash(normalized) if normalized is not None else None,
                    "body_count": len(normalized) if normalized is not None else None,
                    "boundary_outcome": boundary_outcome, "boundary_reason": boundary_reason,
                    "outcome": outcome, "reason": reason}
            candidates.append(item)
            mapping_hashes.add(link["mapping_fingerprint"])

        # Strict duplicate groups choose the lexicographically smallest composite
        # source key. Potential duplicates are held, never automatically removed.
        groups: defaultdict[str, list[dict]] = defaultdict(list)
        for item in candidates:
            if item["outcome"] == "eligible":
                groups[item["comparison"]].append(item)
        strict_duplicates = 0
        for group in groups.values():
            group.sort(key=lambda item: _source_key(item["link"]))
            survivor = group[0]
            for item in group[1:]:
                item["outcome"] = "duplicate"
                item["reason"] = "strict_contact_timestamp_content_match"
                item["survivor"] = _safe_source_reference(_source_key(survivor["link"]))
                strict_duplicates += 1
        eligible = [item for item in candidates if item["outcome"] == "eligible"]
        potential = set()
        for i, left in enumerate(eligible):
            for right in eligible[i + 1:]:
                if left["contact_ref"] != right["contact_ref"]:
                    continue
                same_body = left["body_hash"] == right["body_hash"]
                same_time = left["link"]["activity_timestamp"] == right["link"]["activity_timestamp"]
                try:
                    left_time = datetime.fromisoformat(
                        left["link"]["activity_timestamp"].replace("Z", "+00:00"))
                    right_time = datetime.fromisoformat(
                        right["link"]["activity_timestamp"].replace("Z", "+00:00"))
                    near_time = abs((left_time - right_time).total_seconds()) <= 300
                except (TypeError, ValueError):
                    near_time = False
                similar_body = difflib.SequenceMatcher(
                    None, left["body"], right["body"], autojunk=False).ratio() >= 0.90
                if same_body != same_time or (near_time and similar_body):
                    potential.update((_source_key(left["link"]), _source_key(right["link"])))
        for item in eligible:
            if _source_key(item["link"]) in potential:
                item["outcome"] = "review"
                item["reason"] = "potential_note_duplicate"

        emitted = [item for item in candidates if item["outcome"] == "eligible"]
        exact_selection = {
            "contact_ids": selection.get("contact_ids", []),
            "email_sha256": sorted(email_reference(item) for item in selected_emails),
            "classifications": selection.get("classifications", []),
            "source_types": selection.get("source_types", []),
            "date_from": selection.get("date_from"),
            "date_from_inclusive": selection.get("date_from_inclusive", True),
            "date_to": selection.get("date_to"),
            "date_to_inclusive": selection.get("date_to_inclusive", True),
        }
        selection_fingerprint = hashlib.sha256(json.dumps(
            exact_selection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        counts = {
            "total_discovered_source_activity_contact_pairs": ledger.execute(
                "SELECT COUNT(*) FROM activity_links").fetchone()[0],
            "eligible_notes": len(candidates), "emitted_rows": len(emitted),
            "row_count": len(emitted),
            "unique_source_activities": len({(x["link"]["organisation_id"], x["link"]["source_activity_id"]) for x in emitted}),
            "strict_duplicates_excluded": strict_duplicates,
            "potential_duplicates_held_for_review": len(potential),
            "quoted_histories_safely_trimmed": sum(x["boundary_outcome"] == "trimmed" for x in emitted),
            "ambiguous_trims_held_for_review": held_trim,
            "unmatched_or_ambiguous_contacts": ledger.execute(
                "SELECT COUNT(*) FROM activity_links WHERE status NOT IN ('approved_email_match','shared_email_policy_approved')").fetchone()[0],
        }
        # Persist every considered pair, including review and duplicate outcomes,
        # even when no candidate CSV can safely be created.
        with ledger:
            for item in candidates:
                link = item["link"]
                ledger.execute("""INSERT OR REPLACE INTO note_processing VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NOTE', ?, ?, ?, ?, ?)""",
                    (link["organisation_id"], link["source_contact_id"], link["source_activity_id"],
                     item["raw_hash"], item["raw_count"], item["body_hash"], item["body_count"],
                     BODY_TRANSFORMATION_VERSION, QUOTED_HISTORY_VERSION,
                     item["boundary_outcome"], item["boundary_reason"], item["comparison"],
                     item.get("survivor"), DUPLICATE_POLICY_VERSION, item["outcome"], item["reason"]))
                if item["outcome"] in {"duplicate", "review"}:
                    ledger.execute("""UPDATE activity_links SET status=?, reason_code=?
                        WHERE organisation_id=? AND source_contact_id=? AND source_activity_id=?
                        AND status NOT IN ('submitted','confirmed','rejected','manually_excluded')""",
                        (item["outcome"], item["reason"], link["organisation_id"],
                         link["source_contact_id"], link["source_activity_id"]))
        # Aggregate and fingerprints only: no body, address, subject, or local path.
        print(json.dumps({"selection_fingerprint": selection_fingerprint, "counts": counts}, sort_keys=True))
        if not emitted:
            raise ValueError("selection produced no importable rows; no batch was created")
        if len(mapping_hashes) != 1:
            raise ValueError("selection spans zero or multiple mapping hashes; rediscover first")
        batch_directory.mkdir(parents=False)
        csv_path, manifest_path = batch_directory / "notes.csv", batch_directory / "manifest.json"
        audit_rows = []
        with csv_path.open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for number, item in enumerate(emitted, 2):
                link = item["link"]
                output = {CSV_FIELDS[0]: item["email"], CSV_FIELDS[1]: item["body"],
                          CSV_FIELDS[2]: link["activity_timestamp"]}
                writer.writerow(output)
                audit_rows.append({"csv_row_number": number,
                    "source_key": {"organisation_id": link["organisation_id"],
                                   "source_activity_id": link["source_activity_id"],
                                   "source_contact_id": link["source_contact_id"]},
                    "row_sha256": body_hash(json.dumps(output, sort_keys=True, separators=(",", ":"))),
                    "raw_body_sha256": item["raw_hash"], "raw_character_count": item["raw_count"],
                    "transformed_body_sha256": item["body_hash"],
                    "transformed_character_count": item["body_count"],
                    "boundary_outcome": item["boundary_outcome"],
                    "boundary_reason_code": item["boundary_reason"]})
        csv_hash = file_fingerprint(csv_path)
        source_hash = file_fingerprint(source_path)
        manifest = {"manifest_type": "non_imported_notes_audit_manifest", "batch_id": batch_id,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(), "csv_file": "notes.csv",
            "csv_sha256": csv_hash, "generated_file_hash": csv_hash,
            "environment": environment, "target_portal_label": target_portal_label,
            "selection_filters": exact_selection, "selection_fingerprint": selection_fingerprint,
            "mapping_hash": next(iter(mapping_hashes)), "source_data_fingerprint": source_hash,
            "body_transformation_version": BODY_TRANSFORMATION_VERSION,
            "quoted_history_version": QUOTED_HISTORY_VERSION,
            "duplicate_policy_version": DUPLICATE_POLICY_VERSION,
            "notes_only_policy_version": NOTES_ONLY_POLICY_VERSION,
            "timestamp_contract": "UTC ISO-8601 with Z suffix", "row_count": len(audit_rows),
            "reviewer": None, "review_status": "pending", "result_counts": None,
            "operator_notes": operator_notes, "counts": counts, "rows": audit_rows}
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        now = manifest["generated_at_utc"]
        with ledger:
            ledger.execute("""INSERT INTO batches
                (batch_id,csv_file,csv_sha256,manifest_file,status,created_at_utc,environment,
                 target_portal_label,selection_filters_json,mapping_hash,source_data_fingerprint,
                 row_count,review_status,operator_notes) VALUES (?,?,?,?, 'planned',?,?,?,?,?,?,?,'pending',?)""",
                (batch_id, "notes.csv", csv_hash, "manifest.json", now, environment,
                 target_portal_label, json.dumps(exact_selection, sort_keys=True),
                 manifest["mapping_hash"], source_hash, len(audit_rows), operator_notes))
            for item in audit_rows:
                key=item["source_key"]
                ledger.execute("""INSERT INTO batch_rows
                    (batch_id,csv_row_number,organisation_id,source_activity_id,source_contact_id,row_sha256,state)
                    VALUES (?,?,?,?,?,?,'planned')""", (batch_id,item["csv_row_number"],key["organisation_id"],
                    key["source_activity_id"],key["source_contact_id"],item["row_sha256"]))
        csv_path.chmod(0o444); manifest_path.chmod(0o444)
        return counts
    except Exception:
        if batch_directory.exists():
            for generated in batch_directory.iterdir(): generated.unlink()
            batch_directory.rmdir()
        raise
    finally:
        source.close(); ledger.close()

def record_batch_state(ledger_path: Path, batch_id: str, state: str,
                       import_id: str | None = None, *, reviewer: str | None = None,
                       import_name: str | None = None,
                       result_counts: dict | None = None,
                       operator_notes: str | None = None,
                       operator: str | None = None) -> None:
    """Record review/submission outcomes; confirmation uses evidence commands."""
    allowed = {
        "planned": {"reviewed", "rejected"},
        "reviewed": {"submitted", "rejected"},
        "submitted": {"rejected"},
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
        if state == "submitted" and not ((import_id or "").strip() or
                                          (import_name or "").strip()):
            raise ValueError("submitted state requires an import name or identifier")
        if state == "submitted" and not (operator or "").strip():
            raise ValueError("submitted state requires an operator")
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
                   submitted_by=COALESCE(?, submitted_by),
                   import_started_at_utc=CASE WHEN ?='submitted' THEN ?
                                              ELSE import_started_at_utc END,
                   import_completed_at_utc=CASE WHEN ?='rejected' THEN ?
                                                ELSE import_completed_at_utc END,
                   result_counts_json=COALESCE(?, result_counts_json),
                   operator_notes=COALESCE(?, operator_notes)
                   WHERE batch_id=?""",
                (state, (import_id or "").strip() or None,
                 (reviewer or "").strip() or None, state, state,
                 (import_name or "").strip() or None, (operator or "").strip() or None,
                 state, now, state, now,
                 json.dumps(result_counts, sort_keys=True) if result_counts is not None else None,
                 operator_notes, batch_id),
            )
            row_state = state if state in {"submitted", "rejected"} else "planned"
            connection.execute(
                "UPDATE batch_rows SET state=? WHERE batch_id=?", (row_state, batch_id)
            )
    finally:
        connection.close()


def _sanitized_text(value: str, field: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 1000 or "@" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{field} must be non-empty, single-line, <=1000 characters, and contain no email address")
    return value


def reconcile_manual_import(ledger_path: Path, batch_id: str, *,
                            reported_successful: int, reported_failed: int,
                            checked_by: str, error_files: list[Path],
                            evidence_directory: Path,
                            observation: str | None = None) -> str:
    """Reconcile UI totals and downloaded error CSVs without inventing IDs."""
    if reported_successful < 0 or reported_failed < 0:
        raise ValueError("reported totals cannot be negative")
    checker = _sanitized_text(checked_by, "checked_by")
    note = _sanitized_text(observation, "observation") if observation else None
    connection = open_ledger(ledger_path)
    try:
        batch = connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None or batch["status"] != "submitted":
            raise ValueError("manual reconciliation requires a submitted batch")
        evidence_directory.mkdir(parents=True, exist_ok=True)
        parsed_files, error_rows = [], set()
        aliases = {"row number", "row_number", "row", "csv row number", "csv_row_number"}
        for source_file in error_files:
            digest = file_fingerprint(source_file)
            stored = evidence_directory / f"{batch_id}-{digest[:16]}.csv"
            if not stored.exists():
                shutil.copyfile(source_file, stored)
                stored.chmod(0o444)
            with source_file.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                columns = {name.strip().casefold(): name for name in (reader.fieldnames or [])}
                matches = [columns[name] for name in aliases if name in columns]
                if len(matches) != 1:
                    raise ValueError("each error CSV requires one unambiguous row-number column")
                file_rows = set()
                for item in reader:
                    try:
                        number = int((item[matches[0]] or "").strip())
                    except ValueError as exc:
                        raise ValueError("error CSV row numbers must be integers") from exc
                    file_rows.add(number)
                error_rows.update(file_rows)
                parsed_files.append((stored, digest, len(file_rows)))
        known = {row[0] for row in connection.execute(
            "SELECT csv_row_number FROM batch_rows WHERE batch_id=?", (batch_id,))}
        confident = (reported_successful + reported_failed == batch["row_count"] and
                     len(error_rows) == reported_failed and error_rows.issubset(known))
        outcome = "confirmed_by_import" if confident else "reconciliation_required"
        now = datetime.now(timezone.utc).isoformat()
        with connection:
            cursor = connection.execute(
                """INSERT INTO import_reconciliations
                (batch_id, import_id, import_name, submitted_at_utc, submitted_by,
                 checked_at_utc, checked_by, reported_successful, reported_failed,
                 outcome, observation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, batch["import_id"], batch["hubspot_import_name"],
                 batch["import_started_at_utc"], batch["submitted_by"], now, checker,
                 reported_successful, reported_failed, outcome, note))
            reconciliation_id = cursor.lastrowid
            for stored, digest, count in parsed_files:
                connection.execute("""INSERT INTO import_error_files
                    (reconciliation_id, stored_file, file_sha256, error_row_count)
                    VALUES (?, ?, ?, ?)""", (reconciliation_id, str(stored), digest, count))
            for number in error_rows:
                connection.execute("INSERT INTO import_error_rows VALUES (?, ?, ?)",
                                   (reconciliation_id, batch_id, number))
            connection.execute("UPDATE batches SET status=?, result_counts_json=?, import_completed_at_utc=? WHERE batch_id=?",
                               (outcome, json.dumps({"successful": reported_successful,
                                                    "failed": reported_failed}, sort_keys=True), now, batch_id))
            if confident:
                connection.execute("UPDATE batch_rows SET state='confirmed_by_import' WHERE batch_id=?", (batch_id,))
                if error_rows:
                    connection.executemany("UPDATE batch_rows SET state='rejected' WHERE batch_id=? AND csv_row_number=?",
                                           [(batch_id, number) for number in error_rows])
            else:
                connection.execute("UPDATE batch_rows SET state='reconciliation_required' WHERE batch_id=?", (batch_id,))
        return outcome
    finally:
        connection.close()


def record_stronger_confirmation(ledger_path: Path, batch_id: str, state: str, *,
                                 checked_by: str, selection: dict,
                                 observation: str) -> None:
    """Record privacy-safe export-sample or manual confirmation evidence."""
    if state not in {"confirmed_by_export_sample", "confirmed_manually"}:
        raise ValueError("unsupported stronger confirmation state")
    required = {"contact_reference", "date_from", "date_to", "activity_types"}
    if not required.issubset(selection) or not isinstance(selection["activity_types"], list):
        raise ValueError("selection requires contact_reference, date_from, date_to, activity_types")
    payload = json.dumps(selection, sort_keys=True)
    if "@" in payload:
        raise ValueError("selection must use a sanitized contact reference, not an address")
    connection = open_ledger(ledger_path)
    try:
        if not connection.execute("SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)).fetchone():
            raise ValueError("unknown batch_id")
        with connection:
            connection.execute("""INSERT INTO confirmation_checks
                (batch_id, evidence_state, checked_by, checked_at_utc,
                 selection_json, sanitized_observation) VALUES (?, ?, ?, ?, ?, ?)""",
                (batch_id, state, _sanitized_text(checked_by, "checked_by"),
                 datetime.now(timezone.utc).isoformat(), payload,
                 _sanitized_text(observation, "observation")))
            row_numbers = selection.get("csv_row_numbers", [])
            if row_numbers:
                known = {row[0] for row in connection.execute(
                    "SELECT csv_row_number FROM batch_rows WHERE batch_id=?", (batch_id,))}
                if any(not isinstance(number, int) or number not in known
                       for number in row_numbers):
                    raise ValueError("csv_row_numbers must identify rows in the batch")
                connection.executemany(
                    "UPDATE batch_rows SET state=? WHERE batch_id=? AND csv_row_number=?",
                    [(state, batch_id, number) for number in set(row_numbers)])
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
    state.add_argument("state", choices=("reviewed", "submitted", "rejected"))
    state.add_argument("--import-id")
    state.add_argument("--import-name")
    state.add_argument("--reviewer")
    state.add_argument("--result-counts-json", type=json.loads)
    state.add_argument("--operator-notes")
    state.add_argument("--operator")
    reconciliation = commands.add_parser("reconcile-manual-import")
    reconciliation.add_argument("ledger", type=Path)
    reconciliation.add_argument("batch_id")
    reconciliation.add_argument("evidence_directory", type=Path)
    reconciliation.add_argument("--successful", type=int, required=True)
    reconciliation.add_argument("--failed", type=int, required=True)
    reconciliation.add_argument("--checked-by", required=True)
    reconciliation.add_argument("--error-file", type=Path, action="append", default=[])
    reconciliation.add_argument("--observation")
    confirmation = commands.add_parser("record-stronger-confirmation")
    confirmation.add_argument("ledger", type=Path)
    confirmation.add_argument("batch_id")
    confirmation.add_argument("state", choices=("confirmed_by_export_sample", "confirmed_manually"))
    confirmation.add_argument("--checked-by", required=True)
    confirmation.add_argument("--selection-json", type=json.loads, required=True)
    confirmation.add_argument("--observation", required=True)
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
                           operator_notes=args.operator_notes, operator=args.operator)
    elif args.command == "reconcile-manual-import":
        print(reconcile_manual_import(
            args.ledger, args.batch_id, reported_successful=args.successful,
            reported_failed=args.failed, checked_by=args.checked_by,
            error_files=args.error_file, evidence_directory=args.evidence_directory,
            observation=args.observation))
    elif args.command == "record-stronger-confirmation":
        record_stronger_confirmation(
            args.ledger, args.batch_id, args.state, checked_by=args.checked_by,
            selection=args.selection_json, observation=args.observation)
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
