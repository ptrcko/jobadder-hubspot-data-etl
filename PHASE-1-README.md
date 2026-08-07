# JobAdder → HubSpot Historical Activity Audit

This is Phase 1 of the historical activity migration. It audits a local SQLite
copy of JobAdder data and does not create HubSpot import files.

The SQLite database is opened in read-only mode. The utility writes reports only
to the selected output directory.

## Requirements

- Python 3.10 or later
- Local SQLite database containing:
  - `JobAdderContacts`
  - `JobAdderNotes`
  - `JobAdderNoteContacts`
  - `JobAdderNoteTypes`
- The five recommended local indexes already created

No third-party Python packages are required.

## Files

- `hubspot_history_audit.py` — read-only audit utility
- `activity-mapping.csv` — editable classification rules
- `PHASE-1-README.md` — these instructions

Keep the script and mapping CSV in the same folder.

## Run on Windows

Open PowerShell in the folder containing the script:

```powershell
python .\hubspot_history_audit.py `
  "C:\JobAdderMigration\jobadder-history.db" `
  --output "C:\JobAdderMigration\phase-1-audit"
```

The default exclusive cutoff is:

```text
2026-06-23T00:00:00
```

Only activities before this timestamp are audited.

The utility supports `createdAt` imported into SQLite either as ISO date text or
as Unix milliseconds. DBeaver commonly imports this field as Unix milliseconds;
the audit detects the storage type for each value and applies the same cutoff
correctly.

If `python` is not recognised, try:

```powershell
py .\hubspot_history_audit.py `
  "C:\JobAdderMigration\jobadder-history.db" `
  --output "C:\JobAdderMigration\phase-1-audit"
```

## Optional arguments

```text
--mapping PATH          Mapping CSV location
--output PATH           Report directory
--cutoff TIMESTAMP      Exclusive ISO cutoff
--organisation-id ID    JobAdder organisation ID; default 1
--sample-limit NUMBER   REVIEW samples per type/source; default 10
```

Show all options:

```powershell
python .\hubspot_history_audit.py --help
```

## Reports

### `audit-summary.json`

Machine-readable run summary including the cutoff, index check, classification
totals, quality totals and report names.

### `activity-types.csv`

Every JobAdder type/source combination with activity volumes, date range,
classification and mapping reason.

### `classification-summary.csv`

Projected volumes for:

- `CALL`
- `OUTBOUND_EMAIL`
- `INBOUND_EMAIL`
- `NOTE`
- `EXCLUDE`
- `REVIEW`

These are activity/contact rows, not necessarily unique JobAdder notes.

### `data-quality.csv`

Counts contact links with missing contacts, blank email addresses and blank
activity bodies.

### `shared-activities.csv`

JobAdder notes associated with more than one contact. These need a deliberate
HubSpot association strategy before export.

### `review-samples.csv`

Up to the configured number of recent samples for every unmapped or explicitly
review-required type/source combination. Body previews are limited to 500
characters.

This report can contain personal or confidential communication content. Store it
securely.

## Classification rules

Rules use an exact, case-insensitive match on JobAdder type and source. A source
of `*` matches any source for that type.

Any type/source combination without an explicit rule becomes `REVIEW`. This is
intentional: the utility does not silently treat unknown activity as safe to
import.

Edit `activity-mapping.csv`, rerun the audit, and compare the revised reports.
The SQLite database is not changed.

## Safety

- Keep the SQLite database and reports out of Git.
- Do not store them in cloud-synchronised folders unless appropriately secured.
- Do not run Phase 2 until all `REVIEW` types have been considered.
- The activity cutoff is exclusive to avoid overlap with the integration
  installed on 23 June 2026.
