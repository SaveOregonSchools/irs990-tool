# Federal Audit Clearinghouse offline sidecar

The fraud/risk module can use a local Federal Audit Clearinghouse (FAC)
SQLite sidecar when a live `FAC_API_KEY` is unavailable or when complete,
repeatable historical searches are more important than live freshness.

The sidecar is intentionally separate from `db/irs990.db`:

```text
db/fac_audits.db
```

`build_fac_db.py` creates that file from official public bulk downloads. It
does not require an API key and does not modify the IRS database. Downloads
occur only when an explicit `--download-current` or `--download-historic` flag
is supplied.

## Official sources

The authoritative download pages are:

- 2016-present: <https://www.fac.gov/data/download/current/>
- 2016-present dictionary: <https://www.fac.gov/data/download/current-dictionary/>
- 1998-2015: <https://www.fac.gov/data/download/historic/>
- 1998-2015 dictionary: <https://www.fac.gov/data/download/historic-dictionary/>

Print the stable machine-readable download URLs at any time:

```powershell
.venv\Scripts\python.exe build_fac_db.py --print-download-urls
```

For the dashboard's complete current Single Audit feature set, obtain these
seven public, no-key files:

```text
https://app.fac.gov/dissemination/public-data/gsa/full/general.csv
https://app.fac.gov/dissemination/public-data/gsa/full/federal_awards.csv
https://app.fac.gov/dissemination/public-data/gsa/full/findings.csv
https://app.fac.gov/dissemination/public-data/gsa/full/findings_text.csv
https://app.fac.gov/dissemination/public-data/gsa/full/corrective_action_plans.csv
https://app.fac.gov/dissemination/public-data/gsa/full/additional_eins.csv
https://app.fac.gov/dissemination/public-data/gsa/full/additional_ueis.csv
```

The current full exports can be large. The importer streams them; do not try
to open the complete files in Excel.

For 1998-2015, obtain the Census archive and its published checksum:

```text
https://app.fac.gov/dissemination/public-data/census/csv/census-1998-2015.zip
https://app.fac.gov/dissemination/public-data/census/csv/census-1998-2015.sha1
```

The historical page also offers smaller ZIPs by audit year. The importer
accepts either the combined ZIP or those annual ZIPs. Keep a downloaded
`.sha1` file beside the corresponding `.zip`; the builder validates it before
opening the archive. It also computes and records SHA-256 for every imported
CSV or ZIP member.

## Recommended Python download and build

The built-in downloader is the recommended path on Windows, including Python
3.14 installations where `curl.exe` or PowerShell fails on the FAC certificate
chain. It keeps certificate-authority and hostname verification enabled and
clears only OpenSSL's `VERIFY_X509_STRICT` flag. It accepts only the fixed FAC
URLs printed by `--print-download-urls`; redirects are restricted to the known
FAC endpoint and its public-data S3 path.

Download both eras and build in one command:

```powershell
.venv\Scripts\python.exe build_fac_db.py `
  --download-current `
  --download-historic `
  --download-dir imports\fac `
  --db db\fac_audits.db `
  --source-as-of 2026-08-14
```

Replace the date with the actual download date. Use only
`--download-current` when 1998-2015 history is not needed.

To download and validate without building SQLite:

```powershell
.venv\Scripts\python.exe build_fac_db.py `
  --download-current `
  --download-historic `
  --download-dir imports\fac `
  --download-only
```

Each response is staged as `<filename>.part`. When the server supplies an ETag
or Last-Modified value, an interrupted transfer resumes with `Range` and
`If-Range`. If the object changed or the server ignores ranges, only the
unfinished partial is restarted. Progress is printed to stderr approximately
every 64 MiB.

No selected finished file is replaced until all newly selected downloads have
completed their length and format checks. The historical ZIP is also checked
against the published SHA1 before either refreshed historical file is
published. An invalid or failed refresh therefore leaves the prior finished
files in place.

Refresh downloads and atomically rebuild the sidecar:

```powershell
.venv\Scripts\python.exe build_fac_db.py `
  --download-current `
  --download-historic `
  --download-dir imports\fac `
  --refresh-downloads `
  --db db\fac_audits.db `
  --source-as-of 2026-09-15 `
  --replace
```

Rerun the same command after an interruption to resume `.part` files. If a
partial file or its metadata is known to be bad, add `--restart-downloads`;
that flag removes only unfinished partial state, never a finished download.

The default safety ceiling is 8 GiB per file. It can be raised when an official
export grows, but the CLI will not accept a ceiling below 2 GiB:

```text
--download-max-gib 12
```

## Manual download fallback

`imports/` is ignored by Git and is a suitable local download location:

```powershell
New-Item -ItemType Directory -Force imports\fac\current | Out-Null
New-Item -ItemType Directory -Force imports\fac\historic | Out-Null

curl.exe -L -C - -o imports\fac\current\general.csv https://app.fac.gov/dissemination/public-data/gsa/full/general.csv
curl.exe -L -C - -o imports\fac\current\federal_awards.csv https://app.fac.gov/dissemination/public-data/gsa/full/federal_awards.csv
curl.exe -L -C - -o imports\fac\current\findings.csv https://app.fac.gov/dissemination/public-data/gsa/full/findings.csv
curl.exe -L -C - -o imports\fac\current\findings_text.csv https://app.fac.gov/dissemination/public-data/gsa/full/findings_text.csv
curl.exe -L -C - -o imports\fac\current\corrective_action_plans.csv https://app.fac.gov/dissemination/public-data/gsa/full/corrective_action_plans.csv
curl.exe -L -C - -o imports\fac\current\additional_eins.csv https://app.fac.gov/dissemination/public-data/gsa/full/additional_eins.csv
curl.exe -L -C - -o imports\fac\current\additional_ueis.csv https://app.fac.gov/dissemination/public-data/gsa/full/additional_ueis.csv

curl.exe -L -C - -o imports\fac\historic\census-1998-2015.zip https://app.fac.gov/dissemination/public-data/census/csv/census-1998-2015.zip
curl.exe -L -C - -o imports\fac\historic\census-1998-2015.sha1 https://app.fac.gov/dissemination/public-data/census/csv/census-1998-2015.sha1
```

Use the date on which the files were downloaded as the source snapshot date.
The files are public and do not require a Data.gov or FAC account.

## Build

Build both eras into a new sidecar:

```powershell
.venv\Scripts\python.exe build_fac_db.py `
  --input-dir imports\fac\current `
  --input-dir imports\fac\historic `
  --db db\fac_audits.db `
  --source-as-of 2026-08-14
```

Replace the date with the actual download date. If `--source-as-of` is
omitted, the build date is recorded, but metadata explicitly marks it as an
inferred date rather than a publisher-confirmed date.

To refresh an existing sidecar after downloading a new snapshot:

```powershell
.venv\Scripts\python.exe build_fac_db.py `
  --input-dir imports\fac\current `
  --input-dir imports\fac\historic `
  --db db\fac_audits.db `
  --source-as-of 2026-09-15 `
  --replace
```

The builder never updates the finished sidecar in place. It uses
`db/fac_audits.building.db`, commits a progress checkpoint every 25,000 source
rows, validates the finished schema with `PRAGMA integrity_check`, creates
lookup indexes, and then atomically replaces `db/fac_audits.db`.

If a build is interrupted, rerun the exact same command. Completed source
files are skipped and a partially imported large file resumes after its most
recent committed row checkpoint. (The CSV is scanned forward to that row, but
the committed rows are not reinserted.) The final sidecar remains untouched
until the replacement is complete. If input bytes or the source-as-of date
changed, the builder refuses to mix snapshots. Start a clean staging build in
that case:

```powershell
.venv\Scripts\python.exe build_fac_db.py `
  --input-dir imports\fac\current `
  --input-dir imports\fac\historic `
  --db db\fac_audits.db `
  --source-as-of 2026-09-15 `
  --replace `
  --restart
```

`--restart` deletes only the unfinished staging file associated with the
specified destination. It does not remove or modify the finished sidecar.

## Runtime and verification

The default sidecar is `db/fac_audits.db`. Set `FAC_DB_PATH` before starting
Flask to use another location. `fac_bulk.connect_fac_readonly()` opens it with
SQLite `mode=ro`, `immutable=1`, and `PRAGMA query_only=ON`.

Test a bounded dashboard-shaped lookup:

```powershell
.venv\Scripts\python.exe -c "import json; from fac_bulk import lookup_fac_by_ein; print(json.dumps(lookup_fac_by_ein('123456789'), indent=2, default=str))"
```

Inspect provenance and coverage:

```sql
SELECT key, value
FROM fac_metadata
ORDER BY key;

SELECT source_era, logical_table, audit_year_hint, source_url,
       source_as_of_date, sha256, official_sha1_verified,
       row_count, rejected_count
FROM fac_source_files
ORDER BY source_era, logical_table, audit_year_hint;
```

Normalized tables are:

- `fac_reports`
- `fac_additional_eins` and `fac_additional_ueis`
- `fac_awards`
- `fac_findings`
- `fac_findings_text`
- `fac_corrective_action_plans`
- `fac_rejected_rows`

Every normalized record retains the complete source row, with normalized field
names, in `raw_json`. This keeps fields that are not yet surfaced by the
dashboard available for future research without requiring an immediate
re-import.

Indexes cover primary and additional EIN, UEI, report ID, audit year, fiscal
period, awards, findings, narrative references, and corrective-action
references. `fac_report_eins` provides a union of primary and additional EINs.

## Coverage and limitations

- Current GSA files cover audit years 2016-present and include finding text and
  corrective-action-plan text where those fields are publicly disseminated.
- The Census 1998-2015 format varies across form generations. The builder maps
  its header, award, finding, and additional-identifier tables into the common
  schema and preserves the original fields as JSON.
- The historical bulk archive does not provide separate finding-narrative or
  corrective-action-plan tables. Those fields therefore remain empty for
  1998-2015; `ELECCPAS` means additional CPA/auditor records, not corrective
  action plans.
- FAC states that historical data is provided as-is and that early validation
  quality was more limited. Treat historical matches as research evidence,
  not a definitive compliance determination.
- Public Tribal submissions may omit notes, finding text, and corrective
  action plans when the entity elected statutory suppression. The importer
  cannot reconstruct withheld content.
- The bulk CSVs do not contain audit-report PDFs. The dashboard can link to the
  official FAC-hosted PDF only when a 2016-or-newer report has a validated public
  FAC identifier; it does not download, cache, or extract those PDFs. PDF links
  for historic 1998-2015 records are omitted when no authoritative public
  locator is present.
- Current files are replacement snapshots, not change feeds. Refresh by
  downloading a consistent new set and running an atomic replacement build.
- The importer performs exact EIN matching only. It does not infer identity
  from organization names or addresses.

## Recommended operating sequence

1. Download all seven current files and, if long-run history is needed, the
   historical ZIP plus checksum.
2. Record the download date with `--source-as-of` and build the sidecar.
3. Review `fac_rejected_rows`, source row counts, and coverage metadata.
   The coverage JSON also reports detail rows whose report identifier was not
   present in the supplied general/header files.
4. Run the test suite and verify several known EINs through
   `lookup_fac_by_ein`.
5. Set `FAC_DB_PATH` only when the sidecar is stored outside `db/` and restart
   the Flask process so it opens the completed file.
6. Retain the live FAC API adapter for the newest submissions; use the sidecar
   as the no-key fallback and complete historical index.
