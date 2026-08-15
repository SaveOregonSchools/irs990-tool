# Public Screening Data Sidecar

The **build_screening_sidecar.py** utility builds a regenerable SQLite sidecar
containing public IRS status files, OFAC sanctions lists, and the HHS-OIG List
of Excluded Individuals/Entities (LEIE). It never writes to the main Form 990
database.

The sidecar is intended for risk-research leads. A name match is not a
confirmed identity match and should never be described as one.

## Sources and access

All four source families are official, public downloads. They require no
account, API key, subscription, or special API license:

| Dataset | Official landing page | Imported files | Snapshot meaning |
|---|---|---|---|
| IRS Publication 78 | [TEOS bulk downloads](https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-bulk-data-downloads) | data-download-pub78.zip | Organizations currently listed as eligible to receive tax-deductible contributions. |
| IRS automatic revocation | [TEOS bulk downloads](https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-bulk-data-downloads) | data-download-revocation.zip | Automatic-revocation records, including revocation, posting, and reinstatement dates supplied by IRS. |
| OFAC sanctions | [Sanctions List Service](https://ofac.treasury.gov/sanctions-list-service) | SDN and consolidated primary, alias, address, and comments CSV files | Current SDN and non-SDN consolidated lists. |
| HHS-OIG LEIE | [LEIE database downloads](https://oig.hhs.gov/exclusions/leie-database-supplement-downloads/) | UPDATED.csv | All exclusions currently in effect; reinstated entries have been removed. |

The publishers make these files available specifically for bulk download and
local database use. The builder does not add a license or grant rights beyond
the agencies' terms and applicable law.

The direct URLs and formats were rechecked against the official sites on
August 14, 2026. OFAC's [technical notice](https://ofac.treasury.gov/sdn-list-data-formats-data-schemas/ofac-technical-actions-in-reverse-chronological-order/20240516_44)
requires automated requests to send a User-Agent; the downloader does so.
OFAC also warns that all four relational files are needed for complete legacy
CSV data, so the builder refuses to treat only the primary file as a complete
series.

## First build

From the project root:

~~~powershell
.venv\Scripts\python.exe build_screening_sidecar.py --download
~~~

Defaults:

- downloads are cached under downloads/screening/;
- the completed sidecar is db/screening_data.db;
- IRS, HHS-OIG, SDN, and consolidated non-SDN sources are all included;
- both locations are ignored by Git.

Successful downloads are stored in immutable version directories below
downloads/screening/versions/. One small current-manifest file per family
selects the active IRS, HHS, or OFAC version.

To configure other local locations:

~~~powershell
$env:IRS_SCREENING_CACHE_DIR = "D:\irs990-data\screening-downloads"
$env:IRS_SCREENING_DB_PATH = "D:\irs990-data\screening_data.db"
.venv\Scripts\python.exe build_screening_sidecar.py --download
~~~

The download cache may contain roughly 100 MB of compressed/CSV data, varying
with each release. The expanded SQLite sidecar will be larger.

## Monthly refresh

IRS and HHS-OIG normally replace their complete files monthly. OFAC may change
more often. Re-download every selected file and atomically rebuild:

~~~powershell
.venv\Scripts\python.exe build_screening_sidecar.py --refresh-downloads
~~~

Download and validate without changing the database:

~~~powershell
.venv\Scripts\python.exe build_screening_sidecar.py --refresh-downloads --download-only
~~~

Build or refresh one family only:

~~~powershell
.venv\Scripts\python.exe build_screening_sidecar.py --source irs --refresh-downloads
.venv\Scripts\python.exe build_screening_sidecar.py --source hhs --refresh-downloads
.venv\Scripts\python.exe build_screening_sidecar.py --source ofac --refresh-downloads
~~~

A source-specific build replaces the entire output sidecar with only that
family. Do not use it to incrementally add a family to an existing sidecar.
The normal all-source command is the production refresh command.

## Interruption and failure behavior

- Downloads use a .part file and resume with a validator-bound HTTP byte-range
  request. If-Range uses the prior strong ETag or Last-Modified value.
- A server that does not support ranges causes a safe restart of that one
  partial file. HTTP 416 never publishes a partial as complete.
- Redirects must remain on the selected official HTTPS host, port, and path.
  OFAC may redirect to its fixed government-cloud publication host, but only
  the expected dated publication path, filename, and presigned query fields
  are accepted.
- Each completed download is validated and renamed atomically.
- SHA-256, official URL, retrieval timestamp, HTTP last-modified source date,
  byte count, final URL, and ETag (when supplied) are recorded beside each
  cached file.
- The database is built in a uniquely named temporary file. Schema validation,
  foreign keys, and SQLite quick_check must succeed before the production
  sidecar is replaced.
- If parsing or validation fails, the prior production sidecar remains
  unchanged.
- Every component in a family is staged together. OFAC primary, alias, address,
  and comments schemas and parent/child keys must all validate before one
  atomic manifest switch publishes the new family version. An interruption
  therefore leaves the prior complete version active.
- Each official file is capped at 250 MB to prevent an unexpected response
  from exhausting local storage.

The downloader keeps certificate and hostname verification enabled. On Python
3.14 it clears only OpenSSL's new strict-chain flag because the current federal
certificate chains otherwise fail that added constraint in this environment.

## Manual/offline files

For a manual build, use a separate empty cache directory and place the official
files directly in it using these exact names:

~~~text
irs_pub78.zip
irs_auto_revocation.zip
hhs_leie.csv
ofac_sdn_primary.csv
ofac_sdn_alias.csv
ofac_sdn_address.csv
ofac_sdn_comments.csv
ofac_consolidated_primary.csv
ofac_consolidated_alias.csv
ofac_consolidated_address.csv
ofac_consolidated_comments.csv
~~~

Then build without --download:

~~~powershell
.venv\Scripts\python.exe build_screening_sidecar.py --cache-dir D:\manual-screening-cache
~~~

Manually copied files have no trustworthy release date unless their generated
.metadata.json files are also copied. Supply explicit dates when known:

~~~powershell
.venv\Scripts\python.exe build_screening_sidecar.py --source-date irs_pub78=2026-08-11 --source-date irs_auto_revocation=2026-08-11 --source-date hhs_leie=2026-08-10 --source-date ofac_sdn=2026-08-07 --source-date ofac_consolidated=2026-08-07
~~~

## Runtime lookup boundary

The **queries/_risk_screening.py** module exposes bounded read-only functions
suitable for the risk dashboard:

~~~python
from queries._risk_screening import lookup_irs_status, lookup_name_candidates

irs = lookup_irs_status("12-3456789")
leads = lookup_name_candidates(
    "Example Charity",
    city="Seattle",
    region="WA",
    country="US",
)
~~~

**lookup_irs_status** accepts only a nine-digit normalized EIN and returns
exact Publication 78 and automatic-revocation records with source provenance.

**lookup_name_candidates** searches only exact conservative normalized primary
or source-alias names in OFAC and LEIE. It performs no substring, phonetic,
edit-distance, or token match. Location is returned as exact, partial,
conflicting, unknown, or not requested evidence; it does not silently discard
a name candidate. Results are capped at 25 and the database connection is
opened with SQLite mode=ro, immutable=1, and query_only.

Set the runtime path when it is not beside IRS_DB_PATH:

~~~text
IRS_SCREENING_DB_PATH=D:/irs990-data/screening_data.db
~~~

## Important limitations

- Publication 78 contains the IRS legal name, not a doing-business-as name.
- The auto-revocation file is not equivalent to current revocation status when
  a reinstatement date is present. The importer labels those rows as
  reinstated_after_auto_revocation.
- IRS says some auto-revocation dates from April 1 through July 14, 2020 should
  be July 15, 2020 because of COVID-19 filing relief. The importer preserves
  the published date and does not silently rewrite source data.
- LEIE's downloadable file omits SSNs and EINs. HHS-OIG says a person or entity
  must be verified through its online search using an SSN or EIN. Consequently
  every LEIE runtime result remains an unscored candidate.
- The full LEIE snapshot is active-only; it is not a historical exclusion and
  reinstatement archive. OIG recommends replacing the full database monthly
  instead of combining a full snapshot with supplement files.
- OFAC names and aliases can be shared by unrelated parties. Review addresses,
  identifiers, programs, list type, remarks, and the current official record
  before escalation.
- Normalization retains legal suffixes and diacritics. It standardizes Unicode,
  case, punctuation, whitespace, and ampersand/AND; a collision is never
  treated as proof of identity.
- The sidecar does not provide continuous sanctions monitoring, legal advice,
  or a substitute for official-source verification.
- Completed version directories are retained for rollback and are not silently
  deleted. Archive or prune inactive versions under the ignored cache only
  after confirming the active manifest and production sidecar are healthy.
- The builder resolves both paths and refuses to use IRS_DB_PATH (or the
  default main Form 990 database) as its sidecar destination.
