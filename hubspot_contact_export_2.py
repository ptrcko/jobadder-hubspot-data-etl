#!/usr/bin/env python3
"""Generate a HubSpot-ready historical activity CSV for one JobAdder contact."""

from __future__ import annotations

import argparse
import csv
import html
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


CALL_HEADERS = [
    "Email <CONTACT email>",
    "Call notes <CALL hs_call_body>",
    "Activity date <CALL hs_timestamp>",
]

EMAIL_HEADERS = [
    "Email <CONTACT email>",
    "Email Direction <EMAIL hs_email_direction>",
    "Email body <EMAIL hs_email_html>",
    "Activity date <EMAIL hs_timestamp>",
]

NOTE_HEADERS = [
    "Email <CONTACT email>",
    "Note body <NOTE hs_note_body>",
    "Activity date <NOTE hs_timestamp>",
]

AUDIT_HEADERS = [
    "jobadder_contact_id",
    "contact_email",
    "jobadder_note_id",
    "jobadder_note_internal_id",
    "jobadder_type",
    "jobadder_source",
    "activity_date_utc",
    "activity_timestamp_ms",
    "classification",
    "mapping_reason",
    "decision",
    "decision_reason",
    "associated_contact_count",
]

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
            "Create a HubSpot historical-activity import CSV for one existing "
            "contact. The SQLite database is opened read-only."
        )
    )
    parser.add_argument("database", type=Path, help="Path to jobadder-history.db")
    parser.add_argument(
        "--contact-id",
        type=int,
        required=True,
        help="JobAdder contactId to export",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=Path(__file__).with_name("activity-mapping.csv"),
        help="Phase 1 mapping CSV (default: beside this script)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("phase-2-contact-test"),
        help="Output directory (default: ./phase-2-contact-test)",
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
    return parser.parse_args()


def open_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite database not found: {resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def parse_iso_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_milliseconds(value) -> int:
    if value is None or value == "":
        raise ValueError("Activity has no createdAt value")
    if isinstance(value, (int, float)):
        number = float(value)
        # Permit Unix seconds as a defensive fallback.
        return int(number if abs(number) >= 100_000_000_000 else number * 1000)
    text = str(value).strip()
    if text.replace(".", "", 1).isdigit():
        number = float(text)
        return int(number if abs(number) >= 100_000_000_000 else number * 1000)
    return int(parse_iso_utc(text).timestamp() * 1000)


def utc_display(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(
        timestamp_ms / 1000, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")


def load_mapping(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Mapping CSV not found: {resolved}")
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
            key = (
                (row["jobadder_type"] or "").strip().casefold(),
                (row["jobadder_source"] or "*").strip().casefold(),
            )
            classification = (row["classification"] or "").strip().upper()
            if classification not in ALLOWED_CLASSIFICATIONS:
                raise RuntimeError(
                    f"Invalid classification on mapping line {line_number}: "
                    f"{classification}"
                )
            if key in mapping:
                raise RuntimeError(f"Duplicate mapping on line {line_number}: {key}")
            mapping[key] = {
                "classification": classification,
                "reason": (row["reason"] or "").strip(),
            }
    return mapping


def classify(mapping, note_type: str | None, source: str | None) -> dict[str, str]:
    type_key = (note_type or "").strip().casefold()
    source_key = (source or "").strip().casefold()
    return (
        mapping.get((type_key, source_key))
        or mapping.get((type_key, "*"))
        or {"classification": "REVIEW", "reason": "No explicit mapping"}
    )


def plain_text_html(value: str | None) -> str:
    escaped = html.escape(value or "", quote=False)
    return escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def labelled_body(row: sqlite3.Row, label: str) -> str:
    activity_type = html.escape((row["type"] or "Unspecified").strip(), quote=False)
    source = (row["source"] or "").strip()
    subject = (row["subject"] or "").strip()
    parts = [f"<strong>{label}: {activity_type}</strong>"]
    if source:
        parts.append(f"Source: {html.escape(source, quote=False)}")
    if subject:
        parts.append(f"<strong>Subject: {html.escape(subject, quote=False)}</strong>")
    parts.append("")
    parts.append(plain_text_html(row["text"]))
    return "<br>".join(parts)


def import_row(row: sqlite3.Row, email: str, classification: str, timestamp_ms: int):
    if classification == "CALL":
        return "calls", {
            "Email <CONTACT email>": email,
            "Call notes <CALL hs_call_body>": labelled_body(row, "JobAdder call"),
            "Activity date <CALL hs_timestamp>": str(timestamp_ms),
        }
    if classification in {"OUTBOUND_EMAIL", "INBOUND_EMAIL"}:
        return "emails", {
            "Email <CONTACT email>": email,
            "Email Direction <EMAIL hs_email_direction>": (
                "EMAIL"
                if classification == "OUTBOUND_EMAIL"
                else "INCOMING_EMAIL"
            ),
            "Email body <EMAIL hs_email_html>": labelled_body(
                row, "JobAdder email"
            ),
            "Activity date <EMAIL hs_timestamp>": str(timestamp_ms),
        }
    if classification == "NOTE":
        return "notes", {
            "Email <CONTACT email>": email,
            "Note body <NOTE hs_note_body>": labelled_body(
                row, "JobAdder activity"
            ),
            "Activity date <NOTE hs_timestamp>": str(timestamp_ms),
        }
    raise ValueError(f"Non-importable classification: {classification}")


def contact_record(connection, organisation_id: int, contact_id: int):
    rows = connection.execute(
        """
        SELECT contactId, firstName, lastName, email
        FROM JobAdderContacts
        WHERE JobAdderOrganisationId = ? AND contactId = ?
        """,
        (organisation_id, contact_id),
    ).fetchall()
    if not rows:
        raise RuntimeError(f"Contact not found: {contact_id}")
    if len(rows) > 1:
        raise RuntimeError(f"Multiple contact records found for contactId {contact_id}")
    contact = rows[0]
    if not (contact["email"] or "").strip():
        raise RuntimeError(f"Contact {contact_id} has no email address")
    return contact


def activity_rows(connection, organisation_id: int, contact_id: int):
    return connection.execute(
        """
        SELECT
            n.Id,
            n.noteId,
            n.type,
            n.source,
            n.subject,
            n.reference,
            n.text,
            n.createdAt,
            (
                SELECT COUNT(DISTINCT nc2.contactId)
                FROM JobAdderNoteContacts nc2
                WHERE nc2.JobAdderOrganisationId = n.JobAdderOrganisationId
                  AND nc2.noteId = n.noteId
            ) AS associated_contact_count
        FROM JobAdderNoteContacts nc
        JOIN JobAdderNotes n
          ON n.JobAdderOrganisationId = nc.JobAdderOrganisationId
         AND n.noteId = nc.noteId
        WHERE nc.JobAdderOrganisationId = ?
          AND nc.contactId = ?
        ORDER BY n.createdAt, n.Id
        """,
        (organisation_id, contact_id),
    )


def run(args: argparse.Namespace) -> tuple[dict[str, Path], Path, dict[str, int]]:
    mapping = load_mapping(args.mapping)
    cutoff_ms = int(parse_iso_utc(args.cutoff).timestamp() * 1000)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    connection = open_read_only(args.database)
    try:
        contact = contact_record(
            connection, args.organisation_id, args.contact_id
        )
        email = contact["email"].strip()
        safe_id = str(args.contact_id)
        import_paths = {
            "calls": output / f"hubspot-contact-{safe_id}-calls.csv",
            "emails": output / f"hubspot-contact-{safe_id}-emails.csv",
            "notes": output / f"hubspot-contact-{safe_id}-notes.csv",
        }
        audit_path = output / f"hubspot-contact-{safe_id}-audit.csv"

        totals = {
            "retrieved": 0,
            "included": 0,
            "excluded": 0,
            "review": 0,
            "blank_body": 0,
            "post_cutoff": 0,
            "duplicate_link": 0,
            "call_rows": 0,
            "email_rows": 0,
            "note_rows": 0,
        }
        seen: set[tuple[str, int]] = set()

        with (
            import_paths["calls"].open(
                "w", encoding="utf-8-sig", newline=""
            ) as call_handle,
            import_paths["emails"].open(
                "w", encoding="utf-8-sig", newline=""
            ) as email_handle,
            import_paths["notes"].open(
                "w", encoding="utf-8-sig", newline=""
            ) as note_handle,
            audit_path.open("w", encoding="utf-8-sig", newline="") as audit_handle,
        ):
            import_writers = {
                "calls": csv.DictWriter(call_handle, fieldnames=CALL_HEADERS),
                "emails": csv.DictWriter(email_handle, fieldnames=EMAIL_HEADERS),
                "notes": csv.DictWriter(note_handle, fieldnames=NOTE_HEADERS),
            }
            audit_writer = csv.DictWriter(audit_handle, fieldnames=AUDIT_HEADERS)
            for writer in import_writers.values():
                writer.writeheader()
            audit_writer.writeheader()

            for row in activity_rows(
                connection, args.organisation_id, args.contact_id
            ):
                totals["retrieved"] += 1
                timestamp_ms = timestamp_milliseconds(row["createdAt"])
                decision = classify(mapping, row["type"], row["source"])
                classification = decision["classification"]
                body = (row["text"] or "").strip()
                dedupe_key = (str(row["noteId"]), args.contact_id)

                if dedupe_key in seen:
                    action = "EXCLUDE"
                    action_reason = "Duplicate JobAdder note/contact link"
                    totals["duplicate_link"] += 1
                elif timestamp_ms >= cutoff_ms:
                    action = "EXCLUDE"
                    action_reason = "On or after integration cutoff"
                    totals["post_cutoff"] += 1
                elif not body:
                    action = "EXCLUDE"
                    action_reason = "Blank activity body"
                    totals["blank_body"] += 1
                elif classification == "EXCLUDE":
                    action = "EXCLUDE"
                    action_reason = decision["reason"]
                    totals["excluded"] += 1
                elif classification == "REVIEW":
                    action = "REVIEW"
                    action_reason = decision["reason"]
                    totals["review"] += 1
                else:
                    action = "INCLUDE"
                    action_reason = decision["reason"]
                    totals["included"] += 1
                    output_type, output_row = import_row(
                        row, email, classification, timestamp_ms
                    )
                    import_writers[output_type].writerow(output_row)
                    totals[f"{output_type[:-1]}_rows"] += 1

                seen.add(dedupe_key)
                audit_writer.writerow(
                    {
                        "jobadder_contact_id": args.contact_id,
                        "contact_email": email,
                        "jobadder_note_id": row["noteId"],
                        "jobadder_note_internal_id": row["Id"],
                        "jobadder_type": row["type"] or "",
                        "jobadder_source": row["source"] or "",
                        "activity_date_utc": utc_display(timestamp_ms),
                        "activity_timestamp_ms": timestamp_ms,
                        "classification": classification,
                        "mapping_reason": decision["reason"],
                        "decision": action,
                        "decision_reason": action_reason,
                        "associated_contact_count": row["associated_contact_count"],
                    }
                )
        return import_paths, audit_path, totals
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    try:
        import_paths, audit_path, totals = run(args)
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1

    print("Contact export complete.")
    print(f"Calls CSV: {import_paths['calls']}")
    print(f"Emails CSV: {import_paths['emails']}")
    print(f"Notes CSV: {import_paths['notes']}")
    print(f"Audit CSV: {audit_path}")
    for key, value in totals.items():
        print(f"{key.replace('_', ' ').title()}: {value:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
