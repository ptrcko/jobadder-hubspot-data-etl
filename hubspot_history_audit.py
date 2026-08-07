#!/usr/bin/env python3
"""Read-only Phase 1 audit for JobAdder contact history in SQLite."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


REQUIRED_COLUMNS = {
    "JobAdderContacts": {"contactId", "email", "JobAdderOrganisationId"},
    "JobAdderNotes": {
        "Id", "noteId", "type", "source", "text", "createdAt",
        "JobAdderOrganisationId",
    },
    "JobAdderNoteContacts": {
        "noteId", "contactId", "JobAdderOrganisationId",
    },
    "JobAdderNoteTypes": {"name", "JobAdderOrganisationId"},
}

EXPECTED_INDEXES = {
    "idx_contacts_org_contact",
    "idx_notecontacts_org_contact_note",
    "idx_notecontacts_org_note_contact",
    "idx_notes_org_date",
    "idx_notes_org_note",
}

ALLOWED_CLASSIFICATIONS = {
    "CALL",
    "OUTBOUND_EMAIL",
    "INBOUND_EMAIL",
    "NOTE",
    "EXCLUDE",
    "REVIEW",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit historical JobAdder contact activities in a local SQLite "
            "copy. The database is opened read-only."
        )
    )
    parser.add_argument("database", type=Path, help="Path to jobadder-history.db")
    parser.add_argument(
        "--mapping",
        type=Path,
        default=Path(__file__).with_name("activity-mapping.csv"),
        help="Activity classification CSV (default: beside this script)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("hubspot-history-audit"),
        help="Output directory (default: ./hubspot-history-audit)",
    )
    parser.add_argument(
        "--cutoff",
        default="2026-06-23T00:00:00",
        help="Exclusive activity cutoff in ISO format",
    )
    parser.add_argument(
        "--organisation-id",
        type=int,
        default=1,
        help="JobAdder organisation ID (default: 1)",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=10,
        help="Maximum REVIEW samples per type/source combination (default: 10)",
    )
    return parser.parse_args()


def open_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite database not found: {resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA temp_store = MEMORY")
    return connection


def validate_cutoff(value: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid ISO cutoff: {value}") from exc
    return value


def cutoff_milliseconds(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def display_timestamp(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(
            float(value) / 1000, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
    text = str(value)
    if text.isdigit():
        return datetime.fromtimestamp(
            int(text) / 1000, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
    return text


def validate_database(connection: sqlite3.Connection) -> dict:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing_tables = sorted(set(REQUIRED_COLUMNS) - tables)
    if missing_tables:
        raise RuntimeError(f"Missing tables: {', '.join(missing_tables)}")

    missing_columns: dict[str, list[str]] = {}
    for table, required in REQUIRED_COLUMNS.items():
        columns = {
            row["name"]
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        missing = sorted(required - columns)
        if missing:
            missing_columns[table] = missing
    if missing_columns:
        details = "; ".join(
            f"{table}: {', '.join(columns)}"
            for table, columns in missing_columns.items()
        )
        raise RuntimeError(f"Missing required columns: {details}")

    indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    return {
        "present": sorted(indexes & EXPECTED_INDEXES),
        "missing": sorted(EXPECTED_INDEXES - indexes),
    }


def load_mapping(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Mapping file not found: {resolved}")

    mapping: dict[tuple[str, str], dict[str, str]] = {}
    with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"jobadder_type", "jobadder_source", "classification", "reason"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise RuntimeError(
                "Mapping CSV requires: jobadder_type, jobadder_source, "
                "classification, reason"
            )
        for line_number, row in enumerate(reader, start=2):
            note_type = (row["jobadder_type"] or "").strip().casefold()
            source = (row["jobadder_source"] or "*").strip().casefold()
            classification = (row["classification"] or "").strip().upper()
            if classification not in ALLOWED_CLASSIFICATIONS:
                raise RuntimeError(
                    f"Invalid classification on mapping line {line_number}: "
                    f"{classification}"
                )
            key = (note_type, source)
            if key in mapping:
                raise RuntimeError(f"Duplicate mapping on line {line_number}: {key}")
            mapping[key] = {
                "classification": classification,
                "reason": (row["reason"] or "").strip(),
            }
    return mapping


def classify(
    mapping: dict[tuple[str, str], dict[str, str]],
    note_type: str | None,
    source: str | None,
) -> dict[str, str]:
    type_key = (note_type or "").strip().casefold()
    source_key = (source or "").strip().casefold()
    return (
        mapping.get((type_key, source_key))
        or mapping.get((type_key, "*"))
        or {
            "classification": "REVIEW",
            "reason": "No explicit mapping",
        }
    )


def write_csv(path: Path, fieldnames: list[str], rows) -> int:
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
            count += 1
    return count


def run_audit(args: argparse.Namespace) -> dict:
    cutoff = validate_cutoff(args.cutoff)
    cutoff_ms = cutoff_milliseconds(cutoff)
    mapping = load_mapping(args.mapping)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    connection = open_read_only(args.database)
    try:
        index_status = validate_database(connection)
        params = (args.organisation_id, cutoff_ms, cutoff)
        cutoff_sql = """
            (
                (TYPEOF(n.createdAt) IN ('integer', 'real')
                 AND n.createdAt < ?)
                OR
                (TYPEOF(n.createdAt) NOT IN ('integer', 'real')
                 AND n.createdAt < ?)
            )
        """

        type_sql = f"""
            SELECT
                COALESCE(n.type, '') AS jobadder_type,
                COALESCE(n.source, '') AS jobadder_source,
                COUNT(*) AS activity_contact_rows,
                COUNT(DISTINCT n.Id) AS distinct_activities,
                MIN(n.createdAt) AS earliest_activity,
                MAX(n.createdAt) AS latest_activity
            FROM JobAdderNotes n
            JOIN JobAdderNoteContacts nc
              ON nc.JobAdderOrganisationId = n.JobAdderOrganisationId
             AND nc.noteId = n.noteId
            JOIN JobAdderContacts c
              ON c.JobAdderOrganisationId = nc.JobAdderOrganisationId
             AND c.contactId = nc.contactId
            WHERE n.JobAdderOrganisationId = ?
              AND {cutoff_sql}
              AND c.email IS NOT NULL
              AND TRIM(c.email) <> ''
            GROUP BY COALESCE(n.type, ''), COALESCE(n.source, '')
            ORDER BY activity_contact_rows DESC
        """
        type_rows = []
        classifications = Counter()
        for row in connection.execute(type_sql, params):
            decision = classify(mapping, row["jobadder_type"], row["jobadder_source"])
            item = dict(row)
            item["earliest_activity"] = display_timestamp(item["earliest_activity"])
            item["latest_activity"] = display_timestamp(item["latest_activity"])
            item.update(decision)
            type_rows.append(item)
            classifications[decision["classification"]] += row["activity_contact_rows"]

        write_csv(
            output / "activity-types.csv",
            [
                "jobadder_type",
                "jobadder_source",
                "classification",
                "reason",
                "activity_contact_rows",
                "distinct_activities",
                "earliest_activity",
                "latest_activity",
            ],
            type_rows,
        )

        classification_rows = [
            {"classification": key, "activity_contact_rows": classifications.get(key, 0)}
            for key in sorted(ALLOWED_CLASSIFICATIONS)
        ]
        write_csv(
            output / "classification-summary.csv",
            ["classification", "activity_contact_rows"],
            classification_rows,
        )

        shared_sql = f"""
            SELECT
                n.noteId AS jobadder_note_id,
                COALESCE(n.type, '') AS jobadder_type,
                COALESCE(n.source, '') AS jobadder_source,
                n.createdAt AS activity_date,
                COUNT(DISTINCT nc.contactId) AS associated_contacts,
                GROUP_CONCAT(DISTINCT LOWER(TRIM(c.email))) AS contact_emails
            FROM JobAdderNotes n
            JOIN JobAdderNoteContacts nc
              ON nc.JobAdderOrganisationId = n.JobAdderOrganisationId
             AND nc.noteId = n.noteId
            JOIN JobAdderContacts c
              ON c.JobAdderOrganisationId = nc.JobAdderOrganisationId
             AND c.contactId = nc.contactId
            WHERE n.JobAdderOrganisationId = ?
              AND {cutoff_sql}
              AND c.email IS NOT NULL
              AND TRIM(c.email) <> ''
            GROUP BY n.noteId, n.type, n.source, n.createdAt
            HAVING COUNT(DISTINCT nc.contactId) > 1
            ORDER BY associated_contacts DESC, n.createdAt
        """
        def shared_rows():
            for row in connection.execute(shared_sql, params):
                item = dict(row)
                item["activity_date"] = display_timestamp(item["activity_date"])
                yield item

        shared_count = write_csv(
            output / "shared-activities.csv",
            [
                "jobadder_note_id",
                "jobadder_type",
                "jobadder_source",
                "activity_date",
                "associated_contacts",
                "contact_emails",
            ],
            shared_rows(),
        )

        quality_sql = f"""
            SELECT
                COUNT(*) AS historical_contact_activity_links,
                SUM(CASE WHEN c.contactId IS NULL THEN 1 ELSE 0 END)
                    AS links_with_missing_contact,
                SUM(CASE
                    WHEN c.contactId IS NOT NULL
                     AND (c.email IS NULL OR TRIM(c.email) = '')
                    THEN 1 ELSE 0 END)
                    AS links_with_blank_contact_email,
                SUM(CASE
                    WHEN c.email IS NOT NULL AND TRIM(c.email) <> ''
                     AND (n.text IS NULL OR TRIM(n.text) = '')
                    THEN 1 ELSE 0 END)
                    AS eligible_links_with_blank_body
            FROM JobAdderNotes n
            JOIN JobAdderNoteContacts nc
              ON nc.JobAdderOrganisationId = n.JobAdderOrganisationId
             AND nc.noteId = n.noteId
            LEFT JOIN JobAdderContacts c
              ON c.JobAdderOrganisationId = nc.JobAdderOrganisationId
             AND c.contactId = nc.contactId
            WHERE n.JobAdderOrganisationId = ? AND {cutoff_sql}
        """
        quality_result = dict(connection.execute(quality_sql, params).fetchone())
        quality_rows = [
            {"check": name, "record_count": int(value or 0)}
            for name, value in quality_result.items()
        ]
        write_csv(
            output / "data-quality.csv",
            ["check", "record_count"],
            quality_rows,
        )

        review_pairs = {
            (
                row["jobadder_type"].strip().casefold(),
                row["jobadder_source"].strip().casefold(),
            )
            for row in type_rows
            if row["classification"] == "REVIEW"
        }
        sample_rows = []
        if review_pairs and args.sample_limit > 0:
            pair_conditions = " OR ".join(
                "(LOWER(TRIM(COALESCE(n.type, ''))) = ? "
                "AND LOWER(TRIM(COALESCE(n.source, ''))) = ?)"
                for _ in review_pairs
            )
            pair_params = [
                value
                for pair in sorted(review_pairs)
                for value in pair
            ]
            sample_sql = f"""
                WITH RankedSamples AS
                (
                    SELECT
                        COALESCE(n.type, '') AS jobadder_type,
                        COALESCE(n.source, '') AS jobadder_source,
                        n.noteId AS jobadder_note_id,
                        n.createdAt AS activity_date,
                        n.subject AS subject,
                        n.reference AS reference,
                        REPLACE(REPLACE(
                            SUBSTR(COALESCE(n.text, ''), 1, 500),
                            CHAR(13), ' '), CHAR(10), ' ') AS body_preview,
                        ROW_NUMBER() OVER (
                            PARTITION BY
                                LOWER(TRIM(COALESCE(n.type, ''))),
                                LOWER(TRIM(COALESCE(n.source, '')))
                            ORDER BY n.createdAt DESC, n.Id DESC
                        ) AS sample_number
                    FROM JobAdderNotes n
                    JOIN JobAdderNoteContacts nc
                      ON nc.JobAdderOrganisationId = n.JobAdderOrganisationId
                     AND nc.noteId = n.noteId
                    JOIN JobAdderContacts c
                      ON c.JobAdderOrganisationId = nc.JobAdderOrganisationId
                     AND c.contactId = nc.contactId
                    WHERE n.JobAdderOrganisationId = ?
                      AND {cutoff_sql}
                      AND c.email IS NOT NULL
                      AND TRIM(c.email) <> ''
                      AND ({pair_conditions})
                )
                SELECT
                    jobadder_type,
                    jobadder_source,
                    jobadder_note_id,
                    activity_date,
                    subject,
                    reference,
                    body_preview
                FROM RankedSamples
                WHERE sample_number <= ?
                ORDER BY jobadder_type, jobadder_source, activity_date DESC
            """
            sample_params = (
                args.organisation_id,
                cutoff_ms,
                cutoff,
                *pair_params,
                args.sample_limit,
            )
            for row in connection.execute(sample_sql, sample_params):
                item = dict(row)
                item["activity_date"] = display_timestamp(item["activity_date"])
                sample_rows.append(item)

        write_csv(
            output / "review-samples.csv",
            [
                "jobadder_type",
                "jobadder_source",
                "jobadder_note_id",
                "activity_date",
                "subject",
                "reference",
                "body_preview",
            ],
            sample_rows,
        )

        summary = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "database": str(args.database.expanduser().resolve()),
            "database_opened_read_only": True,
            "organisation_id": args.organisation_id,
            "exclusive_cutoff": cutoff,
            "exclusive_cutoff_unix_milliseconds": cutoff_ms,
            "source_timestamp_storage": "detected per createdAt value",
            "index_status": index_status,
            "activity_type_source_combinations": len(type_rows),
            "classification_activity_contact_rows": dict(sorted(classifications.items())),
            "shared_activities": shared_count,
            "review_samples": len(sample_rows),
            "data_quality": {
                row["check"]: row["record_count"] for row in quality_rows
            },
            "reports": [
                "activity-types.csv",
                "classification-summary.csv",
                "data-quality.csv",
                "shared-activities.csv",
                "review-samples.csv",
            ],
        }
        with (output / "audit-summary.json").open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
            handle.write("\n")
        return summary
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    try:
        summary = run_audit(args)
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"Audit failed: {exc}", file=sys.stderr)
        return 1

    print("Audit complete.")
    print(f"Reports: {args.output.expanduser().resolve()}")
    print(
        "Classified activity/contact rows: "
        f"{sum(summary['classification_activity_contact_rows'].values()):,}"
    )
    print(f"Shared activities: {summary['shared_activities']:,}")
    if summary["index_status"]["missing"]:
        print(
            "Warning: missing recommended indexes: "
            + ", ".join(summary["index_status"]["missing"])
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
