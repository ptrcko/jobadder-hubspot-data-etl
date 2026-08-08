# Source schema and logging contract

## 1. Purpose and authority

This document describes the observed JobAdder SQLite **working-copy** schema and
the logging and audit contract for the historical activity migration. SQLite
type declarations alone do not establish timezone semantics, uniqueness, or
foreign-key enforcement. Each of those properties must be separately verified
before it is relied upon.

The immutable JobAdder source must always be opened with a SQLite URI containing
`mode=ro`, followed immediately after connection by:

```sql
PRAGMA query_only = ON;
```

## 2. Source schema

The supplied working copy reported the following table definitions (HTML tab
entities have been converted to spaces for readability):

```sql
CREATE TABLE JobAdderContacts (
    Id INTEGER,
    contactId INTEGER,
    firstName TEXT,
    lastName TEXT,
    email TEXT,
    "position" TEXT,
    JobAdderOrganisationId INTEGER
);

CREATE TABLE JobAdderNotes (
    Id INTEGER,
    noteId INTEGER,
    "type" TEXT,
    "source" TEXT,
    subject TEXT,
    reference TEXT,
    text TEXT,
    createdAt INTEGER,
    createdById INTEGER,
    createdByName TEXT,
    JobAdderOrganisationId INTEGER
);

CREATE TABLE JobAdderNoteContacts (
    noteId INTEGER,
    contactId INTEGER,
    JobAdderOrganisationId INTEGER
);

CREATE TABLE JobAdderNoteTypes (
    name TEXT,
    JobAdderOrganisationId INTEGER
);
```

The supplied working copy also reported these index definitions:

```sql
CREATE INDEX idx_contacts_org_contact
    ON JobAdderContacts (JobAdderOrganisationId, contactId);
CREATE INDEX idx_notecontacts_org_contact_note
    ON JobAdderNoteContacts (JobAdderOrganisationId, contactId, noteId);
CREATE INDEX idx_notecontacts_org_note_contact
    ON JobAdderNoteContacts (JobAdderOrganisationId, noteId, contactId);
CREATE INDEX idx_notes_org_date
    ON JobAdderNotes (JobAdderOrganisationId, createdAt);
CREATE INDEX idx_notes_org_note
    ON JobAdderNotes (JobAdderOrganisationId, noteId);
```

These indexes were observed on the supplied working copy. They must not be
created, repaired, or rebuilt on the immutable source backup. If indexing is
needed, prepare and identify a separate working copy first.

## 3. Relationships and migration identity

The observed joins are:

* `JobAdderNotes.JobAdderOrganisationId = JobAdderNoteContacts.JobAdderOrganisationId`
* `JobAdderNotes.noteId = JobAdderNoteContacts.noteId`
* `JobAdderContacts.JobAdderOrganisationId = JobAdderNoteContacts.JobAdderOrganisationId`
* `JobAdderContacts.contactId = JobAdderNoteContacts.contactId`

The ledger work-item identity is the composite
`JobAdderOrganisationId + JobAdderNotes.Id + JobAdderNoteContacts.contactId`.
Retain `JobAdderNotes.noteId` as a source business reference, but do not assume
that it is globally unique: the schema declares no unique constraint.

The agreed policy is **one HubSpot activity per associated contact**. HubSpot
activity totals therefore correspond to eligible activity/contact links, not
to the number of unique JobAdder notes.

## 4. Schema limitations

The supplied schema contains activity creator IDs and names, but no users table
and no creator email address. It also has no structured sender, recipient, CC,
BCC, message-ID, email-envelope, call-duration, or meeting start/end fields.
Contact owner, contact creator, and activity creator must not be interpreted as
an email sender without additional evidence.

Contacts without email are not present in HubSpot. Their activity links must
remain in the ledger as unmatched and must not be silently filtered out.

## 5. Timestamp contract

The known validation example is:

* Intended Melbourne time: `2026-04-28 16:21:25 Australia/Melbourne`.
* Equivalent UTC: `2026-04-28 06:21:25Z`.
* Expected Unix milliseconds: `1777357285000`.
* Observed Unix milliseconds: `1777321285000`.
* Observed value resolves to `2026-04-28 06:21:25 Australia/Melbourne`.
* Difference: ten hours.

This strongly suggests that Melbourne wall-clock time was treated as UTC
somewhere before import. The precise origin remains unresolved until the raw
SQLite `createdAt` value is inspected.

Timestamp validation must capture, for several known records spanning both
daylight-saving and standard-time dates:

* JobAdder UI time.
* Raw SQLite `createdAt`.
* SQLite `TYPEOF(createdAt)`.
* Source extraction method.
* Generated CSV value.
* HubSpot import timezone choice.
* HubSpot displayed result.

`Australia/Melbourne` is the candidate source timezone, pending confirmation.
Never add a fixed ten- or eleven-hour offset. Conversion must use IANA timezone
rules so daylight-saving transitions are resolved using the activity date.

Unix milliseconds denote an absolute instant and should not depend on
HubSpot's import timezone selection. If local wall-clock text is emitted
instead, the HubSpot import timezone must be explicitly and consistently set
and documented. The timestamp contract is not approved until representative
records from both AEST and AEDT periods match in JobAdder and HubSpot.

## 6. Ledger timestamp fields

Every discovered activity must preserve:

* `source_timestamp_raw`
* `source_timestamp_storage_type`
* `source_timezone_assumption`
* `source_local_datetime`
* `normalized_timestamp_utc`
* `normalized_timestamp_ms`
* `timestamp_conversion_rule_version`
* `timestamp_validation_status`
* An optional sanitized validation note

The raw source value is immutable evidence. A changed timezone rule must create
a new planned batch and must never rewrite a reviewed or submitted CSV batch.

## 7. Operational event logging

Structured migration events must contain at least:

* UTC event timestamp.
* Log/event schema version.
* Tool version or Git commit.
* Run ID.
* Batch ID, where applicable.
* Command and dry-run/apply mode.
* Environment and non-secret portal label.
* Actor/operator or reviewer identifier.
* Composite source key or privacy-safe reference.
* Previous status and new status.
* Mapping fingerprint.
* Source fingerprint.
* Selection fingerprint.
* CSV file hash and row number, where applicable.
* Import name or identifier.
* Attempt number.
* Outcome and reason code.
* Sanitized error category and message.
* Timestamp conversion rule version.

The SQLite ledger is the authoritative structured audit record. Console and
file logs are operational diagnostics and must never be the only evidence of a
transition. Attempt and state-transition records are append-only; a correction
adds a new event rather than silently changing history.

## 8. Privacy and security

Do not log or commit:

* Full contact email addresses.
* Full email or note bodies.
* JobAdder database files.
* Generated import CSVs.
* Raw HubSpot error files.
* Tokens or credentials.
* Local absolute paths containing user or client information.

Use stable SHA-256 references for contact email when correlation is necessary.
Log body length and a locally computed canonical content hash instead of body
text. Raw source IDs may exist in the private local ledger, but must stay out of
visible HubSpot content and shareable reports. Sanitize exceptions before
logging because HubSpot, SQLite, and CSV errors can embed record values.

## 9. Batch logging and reconciliation

Each immutable batch record must contain:

* Batch ID.
* Selection and selection fingerprint.
* Mapping fingerprint.
* Source fingerprint.
* Timestamp rule version.
* File name, SHA-256 hash, byte size, and row count.
* Review status and reviewer.
* Submission operator and time.
* HubSpot import name or ID.
* Reported successful and failed counts.
* Error-file hash.
* Reconciliation outcome.

A batch with an unknown or partial result becomes
`reconciliation_required` and must never be replayed automatically.

## 10. Minimum reason codes

Reason codes are stable machine-readable values. At minimum, support:

* `source_contact_has_no_email`
* `source_contact_has_invalid_email`
* `source_contact_missing`
* `multiple_source_contacts_share_normalized_email`
* `mapping_review_required`
* `policy_excluded`
* `blank_activity_body`
* `invalid_timestamp`
* `timezone_unverified`
* `duplicate_source_link`
* `already_assigned_to_batch`
* `already_submitted`
* `import_rejected`
* `import_result_inconsistent`
* `manual_exclusion`

## 11. Example sanitized log entries

The following newline-delimited examples use only synthetic identifiers and
hashes. Production events include all fields applicable to their transition;
omitted fields are not applicable, not unknown.

```json
{"event":"discovery","event_at_utc":"2026-08-01T00:00:00Z","event_schema_version":"1","tool_version":"abc1234","run_id":"run-syn-01","command":"discover","mode":"dry-run","environment":"sandbox","portal_label":"test-portal","actor":"operator-01","source_key":"org-7/activity-101/contact-22","previous_status":null,"new_status":"unmatched","mapping_fingerprint":"sha256:1111111111111111","source_fingerprint":"sha256:2222222222222222","selection_fingerprint":"sha256:3333333333333333","attempt_number":1,"outcome":"held","reason_code":"source_contact_has_no_email","timestamp_conversion_rule_version":"tz-v0"}
{"event":"timestamp_validation","event_at_utc":"2026-08-01T00:05:00Z","event_schema_version":"1","run_id":"run-syn-01","source_key":"org-7/activity-102/contact-23","source_timestamp_raw":"2026-01-15 14:30:00","source_timestamp_storage_type":"text","source_timezone_assumption":"Australia/Melbourne","normalized_timestamp_utc":"2026-01-15T03:30:00Z","normalized_timestamp_ms":1768447800000,"timestamp_conversion_rule_version":"tz-v1","timestamp_validation_status":"pending","outcome":"review","reason_code":"timezone_unverified"}
{"event":"batch_generation","event_at_utc":"2026-08-01T01:00:00Z","event_schema_version":"1","run_id":"run-syn-02","batch_id":"batch-syn-01","command":"generate-batch","mode":"dry-run","environment":"sandbox","portal_label":"test-portal","actor":"operator-01","previous_status":"eligible","new_status":"planned","mapping_fingerprint":"sha256:1111111111111111","source_fingerprint":"sha256:2222222222222222","selection_fingerprint":"sha256:3333333333333333","csv_file_hash":"sha256:4444444444444444","csv_row_number":2,"attempt_number":1,"outcome":"generated","reason_code":"ok","timestamp_conversion_rule_version":"tz-v1"}
{"event":"submission","event_at_utc":"2026-08-02T01:00:00Z","event_schema_version":"1","tool_version":"def5678","run_id":"run-syn-03","batch_id":"batch-syn-01","command":"submit-reviewed-batch","mode":"apply","environment":"sandbox","portal_label":"test-portal","actor":"operator-02","previous_status":"reviewed","new_status":"submitted","csv_file_hash":"sha256:4444444444444444","import_id":"import-syn-01","attempt_number":1,"outcome":"accepted","reason_code":"ok","timestamp_conversion_rule_version":"tz-v1"}
{"event":"rejection","event_at_utc":"2026-08-02T01:01:00Z","event_schema_version":"1","run_id":"run-syn-03","batch_id":"batch-syn-01","source_key":"org-7/activity-103/contact-24","previous_status":"submitted","new_status":"rejected","csv_row_number":3,"import_id":"import-syn-01","attempt_number":1,"outcome":"rejected","reason_code":"import_rejected","error_category":"validation","error_message":"Sanitized field validation error","timestamp_conversion_rule_version":"tz-v1"}
{"event":"reconciliation","event_at_utc":"2026-08-02T02:00:00Z","event_schema_version":"1","tool_version":"def5678","run_id":"run-syn-04","batch_id":"batch-syn-01","command":"reconcile","mode":"dry-run","environment":"sandbox","portal_label":"test-portal","actor":"reviewer-01","previous_status":"submitted","new_status":"reconciliation_required","import_id":"import-syn-01","attempt_number":1,"outcome":"inconsistent","reason_code":"import_result_inconsistent","error_file_hash":"sha256:5555555555555555","error_category":"count_mismatch","error_message":"Sanitized reported totals do not match manifest","timestamp_conversion_rule_version":"tz-v1"}
```

## 12. Open decisions

The following remain unresolved:

* Whether JobAdder `createdAt` values represent UTC instants or Melbourne
  wall-clock values.
* Whether native HubSpot emails remain useful when sender, recipient, and owner
  metadata is unavailable.
* Whether ambiguous email types become notes or remain in review.
* Which timestamp representation the final HubSpot CSV will use after sandbox
  validation.
