# Guided import performance roadmap

## Objective

Monthly IRS XML and EO-BMF updates should scale primarily with the data that
changed, while producing the same resolver, candidate, decision, and final
grant outcomes as a full refresh. The application should continue serving the
last validated database while the expensive portion of an update is prepared.

The August 2026 baseline processed 206,589 XML files in approximately 26 hours.
Candidate generation accounted for roughly 20.5 hours: 4,268,343 fast-mode
signatures and 1,813,315 balanced-mode signatures.

## Completed low-risk improvements

- Multiple XML roots are loaded together. Canonical-table rebuilding, index
  maintenance, `ANALYZE`, and `PRAGMA optimize` now run once after the combined
  append instead of once per directory.
- The reviewed deterministic candidate rules run in one scan using the same
  classifier priority and thresholds.
- Enhanced grant statistics are collected once and reused for both
  `grant_match_stats.csv` and the application statistics cache.
- The full source-audit CSV is opt-in. Duplicate reporting and the source
  inventory database remain enabled.
- A redundant `ANALYZE` of the candidate table before the balanced append pass
  is avoided; indexes are still created if missing and are analyzed after the
  full fast load.
- Import logs have timestamps, and `run_summary.json` records every step's
  elapsed time.

These changes do not alter matching inputs, thresholds, scoring, rule priority,
or final database semantics.

## Phase 1: exact batched candidate generation

The current generator can execute up to nine small identity queries per
signature, followed by Python scoring. At full scale this produces tens of
millions of SQLite calls.

Replace only the lookup transport:

1. Stage a bounded batch of signatures in a temporary table.
2. Run the existing EIN, exact-name, address, ZIP, city/state, and token lookup
   predicates as indexed set-based joins.
3. Enforce the existing per-signature limits and ordering with window
   functions.
4. Pass the resulting identity rows through the unchanged Python candidate
   scorer and EIN deduplication.
5. Write candidate rows in bulk.

### Verification gate

- Run the legacy and batched generators against the same frozen fixture and a
  representative production copy.
- Compare sorted candidate rows column-for-column, including rank, identity,
  score, evidence flags, and reason text.
- Compare signature candidate counts and queue statuses.
- Require zero differences before making the batched engine the default.
- Record signatures/second and SQLite read/write volume for fast and balanced
  modes separately.

This phase can reduce full-refresh time without depending on incremental-state
correctness and should be implemented before the larger redesign.

## Phase 2: exact change-impact tracking

Simply removing `--full-refresh` is not safe. A newly loaded return or changed
EO-BMF identity can improve an old unresolved grant, change the best candidate,
or make a formerly unique match ambiguous.

Add durable import metadata:

- Import generation ID and the exact filing IDs/grant IDs added in that run.
- Hashes for each EO-BMF source file and a row-level identity delta containing
  added, changed, and removed identity keys.
- Resolver lookup keys affected by the return/BMF identity delta.
- Candidate-generation version and identity generation attempted for every
  signature, including signatures that produced zero candidates.

Then update the pipeline as follows:

1. Append new filings and record their grant IDs.
2. Upsert return identities for the new filings. When EO-BMF changes, diff its
   stable identity keys rather than treating every BMF row as changed.
3. Re-run the deterministic resolver for new grants plus historical grants
   whose EIN/name/address/geography keys intersect the identity delta. Include
   previously resolved rows whose uniqueness may have changed.
4. Reaggregate only the old and new signature hashes touched by changed
   resolver results. Delete a signature only when its last mapping disappears.
5. Regenerate candidates for new/changed signatures, signatures referencing a
   changed/removed identity, and signatures whose lookup keys intersect a new
   identity. Preserve all other candidate sets.
6. Record balanced-mode attempts with the current identity generation so an
   unchanged zero-candidate signature is not retried every month.
7. Apply triage/rules and applied-layer upserts only to changed signatures and
   their grant mappings.

### Verification gate

For several representative monthly batches:

1. Start two database copies from the same checkpoint.
2. Run the existing full-refresh workflow on one and incremental mode on the
   other.
3. Compare row counts and deterministic content fingerprints for:
   `grant_recipient_resolved`, `org_identity`, `org_identity_token`,
   `grant_recipient_signature`, `grant_recipient_signature_grant`,
   `grant_recipient_ai_candidate`, `grant_recipient_ai_decision`, and
   `grant_recipient_ai_applied`.
4. Perform a full keyed diff for any table whose fingerprint differs.
5. Confirm that pre-existing AI/manual decisions remain attached to the same
   signature and candidate set.
6. Require zero unexplained data differences across at least one XML-only run,
   one EO-BMF-only run, and one combined run.

Keep full refresh as an explicit recovery/audit mode after incremental mode is
enabled.

## Phase 3: short production cutover

Even an optimized SQLite rebuild can block writers or hold exclusive locks.
For predictable availability, prepare the monthly update against versioned
staging databases while the app continues reading the current validated pair.

1. Checkpoint the active main and grant-work databases.
2. Create a versioned staging pair and run the import there.
3. Execute all integrity, row-count, fingerprint, and application smoke tests.
4. Briefly stop new requests, checkpoint the staging pair, switch the configured
   database paths to the validated versions, and restart/reload the app.
5. Retain the previous version only for the agreed rollback window; this is a
   deployment strategy, not an automatic per-import backup policy.

On Windows, use versioned filenames and a controlled app restart rather than
attempting to overwrite an SQLite file that may still have open handles. The
target service interruption should be the cutover/restart window, not the data
processing time.

## Production acceptance targets

- Exact output equivalence to the full-refresh reference workflow.
- Candidate generation no longer dominates a routine monthly update.
- Unchanged zero-candidate signatures are not reprocessed.
- All stage timings and row deltas are present in `run_summary.json`.
- The public app remains on the previous validated database until cutover.
- A failed staging import never changes the active database paths.
