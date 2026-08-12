# Guided IRS data import

The Flask application includes a **Data Maintenance → Import New IRS Data**
module for routine incremental XML updates. It replaces the normal sequence of
PowerShell commands with one validated background workflow.

## Before starting

- Put the new IRS filing XML files in one or more directories. Subdirectories
  are allowed.
- If EO-BMF was updated, download all four files (`eo1.csv` through `eo4.csv`).
- Keep the Flask process open until the workflow completes.
- Ensure no other process is writing to `irs990.db` or
  `grant_matching_work.db`.

The module uses the configured `IRS_DB_PATH` and `IRS_GRANT_WORK_DB_PATH` by
default. Both paths can be reviewed or changed in the form.

## Workflow

Open the app home page and select **Import New IRS Data**. Enter the first XML
batch directory, or select **Browse** to choose it from the in-app directory
browser. When `IRS_XML_ROOT` is configured, the browser starts there and keeps
browsing within that archive. Use **+ Add directory** for each additional XML
batch directory. Manually entered absolute paths are supported even when they
are outside the configured archive.

If EO-BMF changed, select that option and either:

- enter the directory containing the four downloaded CSV files; or
- leave the directory blank if the four project copies were already replaced.

When a source directory is supplied, the module backs up the current EO-BMF
files beneath `eo-bmf/backups/` before installing the replacements.

After confirmation, the module runs these stages in order:

1. Preflight every XML in every incoming directory. All preflights finish before
   any data is changed; an error stops the workflow.
2. Install updated EO-BMF files, when requested.
3. Append each XML directory in order with duplicate prevention.
4. Refresh `irs990_sources.db` when the new XML directory is within the
   configured `IRS_XML_ROOT` archive.
5. Rebuild deterministic grant-recipient resolution.
6. Rebuild the EO-BMF/return identity layer, signatures, fast candidates, and
   balanced candidates.
7. Run reported-EIN triage, nonadjudicable-recipient triage, and every reviewed
   deterministic candidate rule group.
8. Rebuild the applied enhanced-grant layer, write statistics, refresh the web
   statistics cache, and checkpoint both SQLite databases.

Output and step status are shown on the page. Only one import can run in a
Flask process at a time. Failed commands stop the workflow; later stages are not
attempted.

## AI adjudication remains separate

The module deliberately does not call Ollama or another AI system. At successful
completion it displays commands to:

1. export remaining adjudication packets;
2. dry-run the returned decision import;
3. import the reviewed decisions; and
4. rebuild the applied/final layer.

Process the exported packets using the worker procedure in
[Enhanced Grant Matching](grant-matching.md#external-batch-ai-adjudication).
