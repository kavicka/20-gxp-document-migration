# User Requirements Specification: legacy document migration and application retirement

Scope: migrating documents from the legacy file share + metadata export into the target DMS, and retiring the
legacy CTMS application. Every requirement has an ID; [traceability.md](traceability.md) links each ID to the
automated tests that verify it and to the result of the latest run.

Risk: **H** = could put a wrong or corrupted record into the GxP system of record; **M** = could lose
traceability or slow down review; **L** = convenience.

| ID | Requirement | Risk |
|---|---|---|
| URS-01 | Every source document ends in exactly one outcome: loaded, already loaded or quarantined. The run report reconciles source count against these buckets. | H |
| URS-02 | Document type, lifecycle state and country are mapped only through the approved mapping tables in `mapping/`. Unmapped values are quarantined, never guessed. | H |
| URS-03 | Effective dates are read only in the documented legacy formats (ISO, dd/mm/yyyy, d Mon yyyy, yyyymmdd). Unreadable or future dates are quarantined. | H |
| URS-04 | A document is loaded only if its study number has a valid format and exists in the study registry. | H |
| URS-05 | Content integrity: the legacy MD5 is checked before migration, and the SHA-256 of the stored copy is verified after it. | H |
| URS-06 | Content already present (same SHA-256) is not loaded a second time. | M |
| URS-07 | Every load and quarantine is recorded in an audit trail with actor, UTC timestamp and details. The trail is append-only, and any change to a past entry is detectable. | H |
| URS-08 | A dry run produces the same outcomes and report as a real run without changing the target. | M |
| URS-09 | Re-running a batch is idempotent: nothing is loaded twice and the target content does not change. | M |
| URS-10 | Approved documents carry a major version (x.0); a minor version on an approved document is quarantined. | M |
| URS-11 | The retired application's data is exported to open formats (CSV + JSON schema) with a manifest of row counts, checksums and a retention date (25 years). | H |
| URS-12 | Archive completeness and integrity can be verified from the archive alone, without the legacy application. | H |
| URS-13 | Required metadata is enforced: a title for every document, and a country for informed consent forms. | M |
