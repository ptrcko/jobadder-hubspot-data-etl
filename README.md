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

A separate local migration ledger must track every source activity, contact match, deduplication result, batch membership, import result, retry, exclusion, and manual decision. After matching, **Contact Record ID** is the preferred CSV association identifier. Email is only a fallback discovery key and must not become the default import association when a Record ID is known.

## Repository inventory

- `hubspot_history_audit.py` is the read-only Phase 1 audit and classification utility.
- `migration_ledger.py` is the restartable discovery-ledger foundation. It
  retains activity links for source contacts without email, keeps them outside
  import candidates, and produces aggregate preview/reconciliation reporting.
- `hubspot_contact_export.py` is the older legacy, single-contact exporter prototype.
- `hubspot_contact_export_2.py` is the newer single-contact exporter prototype; it is still not production batch tooling.
- `activity-mapping.csv` contains the editable activity classification rules.
- [`PHASE-1-README.md`](PHASE-1-README.md) documents the existing audit command and its reports.
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
`unmatched_contact`, with reason
`source_contact_has_no_email_and_is_not_in_hubspot`. The ledger preserves the
source contact/activity IDs, type, timestamp, mapping decision/reason and
mapping fingerprint, but its schema deliberately has no contact-address field.
The import-candidate boundary only returns links with an approved contact match,
a pre-existing HubSpot Contact Record ID, and an importable mapping decision;
therefore unresolved links cannot enter a generated HubSpot CSV.

Both batch previews and final reconciliation reports include privacy-safe counts
by activity type and current status, with the earliest and latest source
timestamps. Reconsidered links remain in these totals, so the final report does
not erase the fact that they originally lacked source email:

```bash
python migration_ledger.py preview migration-ledger.db batch-preview.json
python migration_ledger.py reconcile migration-ledger.db final-reconciliation.json
```

If a separate, operator-owned process later creates those contacts in HubSpot,
an operator may prepare a reviewed CSV containing only
`source_contact_id,hubspot_contact_id` and explicitly reconsider the links:

```bash
python migration_ledger.py reconsider-unmatched migration-ledger.db approved-matches.csv \
  --confirm-contacts-already-exist-in-hubspot
```

The confirmation is mandatory and the match file must contain nonblank IDs.
This command only records approved associations and state transitions. It has no
HubSpot API write path and never creates, updates, merges, or deletes contacts.

A batch with an unknown or partial import outcome must be reconciled before any retry. Never regenerate and blindly replay it. Generated files are immutable: any correction creates a new batch with a new manifest and hashes.

## Documentation authority

Files under `HubspotDocs/` are reference snapshots and retain their copied “last updated” dates; for example, `format-import-files.md` says **August 6, 2026**, and `import-objects.md` says **July 22, 2026**. They are not authoritative for future runs. Before production, operators must verify the current official HubSpot documentation and test the actual target portal's import behavior, subscription limits, required fields, associations, and deduplication behavior.
