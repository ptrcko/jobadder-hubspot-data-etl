# JobAdder to HubSpot historical activity migration

## Purpose and scope

This repository supports a controlled migration of historical JobAdder activities into **existing HubSpot contacts**. It covers calls, inbound and outbound emails, notes, and meeting-like records where the source data and an approved mapping make that appropriate. It is not a generic contact exporter, and the migration tooling **must never create contacts**.

The current exclusive cutoff is `2026-06-23T00:00:00Z`: only activity before that instant is in scope. The cutoff must remain configurable so that every audit and batch can record the value it actually used.

## Safety model

- Open the JobAdder SQLite database read-only. Never add migration tables or otherwise mutate the source database.
- Store migration state in a separate SQLite ledger.
- Keep databases, generated activity files, reports, logs, credentials, tokens, and all personal or client data out of Git.
- Treat discovery/dry-run and HubSpot creation as separate operations. Discovery may read source data and HubSpot; creation requires a separately reviewed, immutable batch.
- Do not assume that CSV activity import is idempotent. Existing emails, meetings, notes, and tasks cannot be updated through import, so replaying a file can create duplicate activities.

## Chosen architecture

CSV files are the primary bulk-creation mechanism. HubSpot APIs are used for contact resolution, read-only deduplication, and post-import reconciliation—not as an excuse to create contacts.

A separate local migration ledger tracks every source activity, contact match,
deduplication result, batch membership, import result, retry, exclusion, and
manual decision. The chosen CSV association identifier is the contact's
**normalized exact email**. Planning trims surrounding whitespace, case-folds
the address, validates its structure, and hashes that normalized value for the
privacy-safe ledger. Blank or invalid addresses become `unmatched_contact` and
cannot enter a batch.

HubSpot's contact-email uniqueness is only an association mechanism. It says
nothing about whether an activity was previously imported. Replay protection
comes from the local ledger's composite source key, immutable batch ID, CSV and
row hashes, row-level manifest, and evidence-based state transitions. Never
replay a CSV based on email uniqueness.

The migration work item is an activity/contact association identified by the
composite key **`organisation_id + source_activity_id + source_contact_id`**.
Discovery creates one ledger item for every valid association; it must not
collapse associations merely because they share a JobAdder note ID. A shared
JobAdder activity intentionally becomes a separate HubSpot activity for each
associated contact, so every contact receives the historical context.

## Repository inventory

- `hubspot_history_audit.py` is the read-only Phase 1 audit and classification utility.
- `migration_ledger.py` is the restartable discovery-ledger foundation. It
  retains activity links for source contacts without email, keeps them outside
  import candidates, and produces aggregate preview/reconciliation reporting.
- `hubspot_contact_export.py` is the older legacy, single-contact exporter prototype.
- `hubspot_contact_export_2.py` is the newer single-contact exporter prototype; it is still not production batch tooling.
- `activity-mapping.csv` contains the editable activity classification rules.
- [`PHASE-1-README.md`](PHASE-1-README.md) documents the existing audit command and its reports.
- [`SOURCE_SCHEMA_AND_LOGGING.md`](SOURCE_SCHEMA_AND_LOGGING.md) records the observed
  source working-copy schema, timestamp validation contract, and structured
  migration logging requirements.
- `HubspotDocs/` contains copied HubSpot documentation snapshots, not live documentation.
- `audit-summary.json`, `activity-types.csv`, `classification-summary.csv`, and `data-quality.csv` are checked-in aggregate audit artifacts. They contain no row-level content, but may be stale. Do not rely on an audit artifact unless its recorded mapping fingerprint matches the current `activity-mapping.csv`; artifacts without a fingerprint must be treated as unverifiable and regenerated.

Both exporter scripts are prototypes and must not be used as the production migration workflow.

## Current audit findings

The checked-in `audit-summary.json` reports **195,426 historical contact/activity links**. Its classified activity/contact rows total **180,158**, split as follows:

| Classification | Rows |
| --- | ---: |
| `CALL` | 42,912 |
| `OUTBOUND_EMAIL` | 7,488 |
| `INBOUND_EMAIL` | 14,264 |
| `NOTE` | 11,001 |
| `EXCLUDE` | 102,172 |
| `REVIEW` | 2,321 |

It also reports **3,068 shared activities**, **13,931 links with blank contact email**, **1,337 links with a missing contact**, and **850 otherwise eligible links with a blank body**. In particular, the 2,321 review rows require classification decisions, while shared activities and data-quality failures require explicit handling. These findings prevent an unreviewed full import.

## Activity classification

Classification uses an exact, case-insensitive match on the pair `(jobadder_type, jobadder_source)`, after trimming surrounding whitespace. An exact source rule takes precedence over a wildcard (`*`) source rule for the same type. A pair with no applicable rule becomes `REVIEW`; it must never be silently included.

Every generated batch requires an approved mapping and must record a reproducible mapping hash/version in both its ledger records and manifest. A mapping change invalidates approval of any not-yet-imported batch generated from the previous mapping.

## CSV constraints

The copied guidance in `HubspotDocs/format-import-files.md` says import files must have one sheet, fewer than 1,000 columns, and UTF-8 encoding when they contain foreign-language characters. Generate all migration CSVs as UTF-8. For a paid Smart CRM or Starter, Professional, or Enterprise account, each file is limited to **512 MB** and **1,048,576 rows**. The rolling 24-hour limits are **500 imports and 10,000,000 rows** through the UI, or up to **80,000,000 rows** through the imports API. Confirm the target subscription and current limits before planning batches.

Required activity fields are:

- calls: call notes;
- emails: email body and email direction;
- notes: note body; and
- meetings: meeting description, meeting start time, and meeting end time.

Always provide the activity timestamp, even where HubSpot describes it as recommended rather than required, so imported history retains its original chronology.

Batch generation emits one CSV row for each eligible activity/contact pair and
each row contains exactly one contact email. Source identifiers are not placed
in the imported CSV. Instead, the separate, non-imported audit manifest maps
the physical CSV row number (including the header as row 1) to the composite
source key. Preview, manifest, and reconciliation counts report these three
quantities independently:

- **unique source activities**, counting a shared activity once;
- **activity/contact pairs**, counting every discovered association; and
- **expected HubSpot activity creations**, counting eligible pairs, because
  each pair produces its own HubSpot activity.

Generate a new batch directory (the command refuses an existing directory or a
previously used batch ID). It prints the exact normalized selection and selected
row/activity counts **before** creating the directory or CSV:

```bash
python migration_ledger.py generate-batch jobadder-history.db migration-ledger.db \
  batches/reviewed-2026-001 --batch-id reviewed-2026-001 \
  --environment sandbox --target-portal-label migration-test-portal \
  --contact-id 123 --email reviewed@example.test --classification CALL \
  --source-type User --date-from 2025-01-01T00:00:00Z \
  --date-to 2025-01-31T23:59:59Z
```

Contact IDs and emails may be repeated. Classification and source-type filters
may also be repeated. Date boundaries are inclusive by default; use
`--date-from-exclusive` and/or `--date-to-exclusive` when required. A changed
selection or mapping always requires a new directory and batch ID. Each manifest
and ledger batch record includes environment, target portal label, the complete
selection (emails represented by normalized SHA-256 references, never addresses),
mapping hash, source-data fingerprint, generated CSV hash, row count,
review metadata, HubSpot import name/ID and dates, result counts, and operator
notes. The CSV and manifest are made read-only after successful generation.

The audit manifest is a review and reconciliation sidecar only. Never submit it
to HubSpot or combine its composite source keys with visible activity content.

## Content policy

Visible activity bodies must not add `JobAdder call`, `JobAdder email`, `JobAdder activity`, source IDs, migration references, or similar migration labels. This restriction applies to migration-added text: legitimate original content that mentions JobAdder must remain unchanged.

The `labelled_body()` behavior in `hubspot_contact_export_2.py` adds prototype labels and source metadata. It **must not** be used for production output.

## Operating sequence

1. **Audit** the read-only source using the command in [`PHASE-1-README.md`](PHASE-1-README.md).
2. **Classify** every type/source pair and approve the mapping.
3. **Match contacts** against existing HubSpot contacts without creating any.
4. **Deduplicate** against existing HubSpot activity using read-only API queries.
5. **Plan** batches, exclusions, exceptions, and manual decisions in the ledger.
6. **Generate an immutable batch** plus a manifest containing the cutoff, mapping hash/version, row counts, source/query parameters, file names, and cryptographic hashes for every generated file.
7. **Review** the batch, manifest, unresolved items, and portal import settings.
8. **Import once** and record the import identifiers and outcome in the ledger.
9. **Reconcile** HubSpot results against the manifest and ledger.

## Contacts without source email

Future ledger discovery must not filter source links on `JobAdderContacts.email`.
Run the restartable discovery command instead; it opens JobAdder with SQLite
`mode=ro` and `query_only`, and writes state only to a separate ledger:

```bash
python migration_ledger.py discover jobadder-history.db migration-ledger.db \
  --mapping activity-mapping.csv --cutoff 2026-06-23T00:00:00Z
```

A link whose JobAdder contact has no email is retained as
`unmatched_contact`, with reason `source_contact_has_no_email`; an invalid
address uses `source_contact_has_invalid_email`. The ledger preserves the
source contact/activity IDs, type, timestamp, mapping decision/reason and
mapping fingerprint, but its schema deliberately has no contact-address field.
The import-candidate boundary only returns links with a validated exact-email
association (and, where applicable, an approved shared-email policy) plus an importable mapping decision;
therefore unresolved links cannot enter a generated HubSpot CSV.

Both batch previews and final reconciliation reports include privacy-safe counts
by activity type and current status, with the earliest and latest source
timestamps. Reconsidered links remain in these totals, so the final report does
not erase the fact that they originally lacked source email:

```bash
python migration_ledger.py preview migration-ledger.db batch-preview.json
python migration_ledger.py reconcile migration-ledger.db final-reconciliation.json
```

## Shared source emails

Planning compares normalized addresses across source contact IDs. If multiple
JobAdder contact IDs share one normalized email, all of their rows become
`shared_email_exception` before production batch generation. Preview and
reconciliation JSON include an exception report containing the source contact
IDs and a SHA-256 email reference—never the address itself.

An operator must document and review a policy CSV with
`email_sha256,decision`, where the decision is `approve_import` or `exclude`.
No shared-email row is eligible until the explicit confirmation is supplied:

```bash
python migration_ledger.py decide-shared-emails migration-ledger.db shared-policy.csv \
  --confirm-reviewed-policy
```

This command only records the policy outcome. It has no HubSpot API write path
and never creates, updates, merges, or deletes contacts.

After reviewing a generated manifest, record submission separately. Manual UI
imports may have either an import name or identifier, but always require the
operator; the ledger retains that value, submission time, batch ID, immutable
file hash, and every physical CSV row number:

```bash
python migration_ledger.py record-batch-state migration-ledger.db reviewed-2026-001 reviewed --reviewer initials
python migration_ledger.py record-batch-state migration-ledger.db reviewed-2026-001 submitted --import-id 12345 --import-name sandbox-acceptance-001 --operator initials
```

Download every HubSpot error CSV and reconcile it with the reported successful
and failed totals. The command copies each error file into a local immutable
evidence directory and records its hash; these files and the ledger are
sensitive local artifacts and must not be committed:

```bash
python migration_ledger.py reconcile-manual-import migration-ledger.db reviewed-2026-001 local-import-evidence/reviewed-2026-001 --successful 9 --failed 1 --checked-by initials --error-file hubspot-errors.csv
```

Rows present in the error files become `rejected`. Rows absent from those files
become `confirmed_by_import` only when successful + failed equals the manifest
row count, the unique error row count equals the reported failed total, and
every error row number belongs to the batch. Any discrepancy marks the whole
batch and its rows `reconciliation_required`; it must not be retried.
`hubspot_activity_id` is nullable and is neither fabricated nor required.

Optional `confirmed_by_export_sample` and `confirmed_manually` checks are stored
as independent evidence. They record the checker and time, a sanitized contact
reference (never an address), date/type range, and sanitized observations:

```bash
python migration_ledger.py record-stronger-confirmation migration-ledger.db reviewed-2026-001 confirmed_by_export_sample --checked-by initials --selection-json '{"contact_reference":"sha256:synthetic","date_from":"2025-01-01","date_to":"2025-01-31","activity_types":["CALL"]}' --observation 'Synthetic sample matched the reviewed timestamp and type'
```

A batch with an unknown or partial import outcome must be reconciled before any retry. Never regenerate and blindly replay it. Generated files are immutable: any correction creates a new batch with a new manifest and hashes.

## Sandbox acceptance checklist

Before approving any production batch, create a small, synthetic/sanitized batch
for the named sandbox portal and retain aggregate evidence that all items pass:

- [ ] **Contact association:** every activity is attached to the intended existing
  contact and no contact was created, updated, merged, or deleted.
- [ ] **Shared activities:** the approved one-activity-per-associated-contact policy
  produces the expected separate activities and no unintended association.
- [ ] **Direction:** inbound and outbound email/call direction is displayed correctly.
- [ ] **Timestamps:** UTC conversion, dates, times, and ordering match the source,
  including boundary selections.
- [ ] **Visible content:** bodies are preserved without migration labels, IDs, or
  other injected text.
- [ ] **Unicode:** non-ASCII text and CSV UTF-8 encoding survive unchanged.
- [ ] **Long bodies:** approved maximum-length samples import without silent
  truncation; any portal limit is documented.
- [ ] **Import errors:** rejected rows and partial/unknown outcomes are recorded and
  reconciled before considering a replacement batch.
- [ ] **Local replay prevention:** attempting to reuse the batch ID or directory is
  refused, and a repeat-run/no-duplicate check confirms submitted source keys are
  not selected into another batch.

## Documentation authority

Files under `HubspotDocs/` are reference snapshots and retain their copied “last updated” dates; for example, `format-import-files.md` says **August 6, 2026**, and `import-objects.md` says **July 22, 2026**. They are not authoritative for future runs. Before production, operators must verify the current official HubSpot documentation and test the actual target portal's import behavior, subscription limits, required fields, associations, and deduplication behavior.
