# Repository Instructions

These instructions apply to the entire repository. Future contributors and coding agents must preserve the following migration safeguards in code, tests, generated artifacts, and documentation.

## Project goal

- Migrate historical JobAdder contact activity into existing HubSpot contacts.
- Use CSV import as the primary bulk-write path. Use APIs only for read-only discovery, contact resolution, deduplication, and reconciliation.
- Design every migration workflow to be restartable, auditable, and safe against repeated execution.

## Immutable JobAdder source

- Never modify, migrate, vacuum, reindex, attach writable objects to, or create tables in the JobAdder source database.
- Always open the source database through a SQLite URI containing `mode=ro`, and enable `PRAGMA query_only` immediately after connecting. Tests must verify both protections.
- Create indexes only on a separately prepared working copy. Never assume permission to alter the original backup.

## HubSpot write safety

- Do not create, update, merge, or delete HubSpot contacts.
- Keep discovery and deduplication commands technically separate from import submission commands.
- Make every new workflow dry-run by default.
- Permit a production import only through an explicit apply/import command that confirms the target portal and requires both a reviewed batch manifest and approved contact matches.
- Never retry a CSV import whose outcome is unknown or partial. First reconcile the import and prove which rows were created.

## Ledger and idempotency

- Record every source activity in a separate SQLite ledger, including `excluded`, `review`, `unmatched`, `ambiguous`, `duplicate`, `submitted`, `confirmed`, `rejected`, `retried`, and `manually_excluded` outcomes.
- Never claim that HubSpot CSV activity imports are idempotent.
- Persist stable source keys, mapping fingerprints, batch IDs, file hashes, import IDs, attempt records, and state transitions.
- Treat generated CSV batches as immutable after review or submission; create a new batch rather than rewriting one.

## Contact matching

- Resolve contacts first through an approved cached mapping or a configured JobAdder Contact ID property, and then through exact email matching.
- Where HubSpot supports it, generate CSV associations with the HubSpot Contact Record ID.
- Never guess when multiple HubSpot contacts are possible. Route all unmatched and ambiguous contacts to review.

## Deduplication

- Check the local ledger before querying HubSpot.
- Compare existing HubSpot activities using contact, activity type, timestamp tolerance, normalized content, and type-specific metadata.
- Automatically skip only strict, high-confidence duplicates. Hold every potential duplicate for manual review.
- Do not add source identifiers or migration labels to visible activity bodies to make deduplication easier.

## Content handling

- Do not add `JobAdder call`, `JobAdder email`, `JobAdder activity`, JobAdder IDs, source references, or migration labels to visible HubSpot content.
- Do not globally delete the word `JobAdder`; preserve legitimate original content exactly.
- Treat `labelled_body()` in `hubspot_contact_export_2.py` as non-production prototype behavior. Do not use it in a production path.
- Do not log or commit full activity bodies or contact addresses.

## Activity mapping

- Treat `activity-mapping.csv` as controlled migration configuration.
- Match exact normalized type and source values, evaluating exact-source rules before wildcard rules.
- Route unknown types to `REVIEW`; never silently assign a default.
- Include an approved mapping version or hash in every audit record and batch manifest.
- Document the business rationale whenever a mapping decision changes.

## Sensitive files and Git

- Never commit SQLite databases or sidecars, generated import CSVs, per-contact audit files, raw review samples, credentials, `.env` files, access tokens, personal data, or full content payloads.
- Commit aggregate reports only after verifying that they contain neither personal data nor sensitive local paths.
- Sanitize local paths in generated summaries before committing them.
- Read HubSpot credentials from environment variables and provide only credential-free sample configuration.

## Testing expectations

- Use only synthetic or explicitly sanitized fixtures.
- Test mapping precedence, timestamp conversion, direction values, CSV quoting and encoding, content preservation, duplicate detection, batch manifests, interruption recovery, and reconciliation.
- Before production, require a small test-portal import followed by a repeat-run/no-duplicate acceptance check.
- Never run a production import from an automated test.

## Documentation snapshots

- Treat files under `HubspotDocs/` as dated reference snapshots, not as an automatically current API contract.
- Recheck the official HubSpot documentation and the target portal's current import behavior before changing import formats or limits.

## Change discipline

- Keep legacy and Phase 2 prototype scripts clearly labeled until replacement behavior has been validated.
- Never silently alter historical audit artifacts.
- When regenerated artifacts differ, record the source fingerprint, cutoff, mapping fingerprint, tool version, and reason for regeneration.
