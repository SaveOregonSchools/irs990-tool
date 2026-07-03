# Query modules

The `queries/` folder contains the prebuilt query modules shown in the Flask research console.

`app.py` auto-discovers Python files in this folder, imports them, and registers modules that expose the required plugin interface. While the app is running, it checks query file modification times on each request and reloads the registry when a query module is added or edited.

---

## Required module interface

Every query module should define:

```python
META = {
    "key": "unique_query_key",
    "name": "Human-readable query name",
    "description": "What the query does.",
}

HEADERS = ["col1", "col2"]
META["headers"] = HEADERS

def render_fields(form) -> str:
    ...

def run(form):
    return HEADERS, rows

def export_rows(form):
    return rows_or_iterator
```

Optional:

```python
def export_headers(form):
    return headers
```

Use `export_headers()` when the CSV export should omit helper columns that are useful in the preview UI, such as a generated SQL column.

---

## Current modules

| File | Purpose |
|---|---|
| `ask_database.py` | Ask a plain-English database question, generate validated SQL, run a preview, or validate/run existing SQL. |
| `filings_by_ein.py` | List canonical filings for one or more EINs. |
| `ngo_core_data.py` | Return core organization, filing, financial, address, mission, tax-status, and indicator fields. |
| `ngo_ein_by_name.py` | Find EINs from pasted organization names using deterministic normalized matching and optional fuzzy fallback. |
| `ngo_grants_out.py` | List grants paid by filer/grantor organizations. |
| `ngo_grants_in.py` | List grants received by recipient EINs. |
| `ngo_grants_io.py` | Combined paid/received grant workflow with dedupe and row caps. |
| `ngo_contractors_out.py` | List contractor/vendor payments reported by filers. |
| `lobbying_political_activity.py` | Explore expanded Schedule C lobbying, political campaign, 527, dues/proxy-tax, and 990-PF indicators. |
| `ngo_related_orgs_sched_r.py` | Return Schedule R related organization entries by EIN, filer state, and year range. |
| `people_lookup.py` | Search person names across officers, highly compensated employees, contractors, grant recipients, return headers, books-in-care-of, Schedule J, Schedule L, and 990-PF officer data. |

---

## Query design conventions

### Use read-only connections

Import database helpers from `common.py`:

```python
from common import connect_ro, normalize_eins
```

Use `connect_ro()` for query modules. Query modules should not mutate the database.

### Prefer canonical filings for year-based research

For most filing/year questions, start from:

```sql
FROM canonical_by_ein_year c
JOIN returns r ON r.filing_id = c.filing_id
```

Add `core_hot`, `grants_compat_v1`, `vw_contractors`, `officers`, or `sched_r_related_orgs_expanded` as needed.

### Use parameterized SQL

Do not inject user-provided values directly into SQL strings. Use placeholders:

```python
cur = conn.execute("SELECT * FROM returns WHERE state = ?", [state])
```

For EIN lists, chunk large `IN (...)` lists. Most existing modules use chunks of around 300 EINs.

### Keep preview and export behavior separate

`run(form)` should return rows for the UI preview. `app.py` applies the preview row limit.

`export_rows(form)` should return the full intended export, preferably as an iterator for large results.

### Use user-facing metadata

`META["name"]` and `META["description"]` are displayed in the UI. Keep them readable for researchers, not just developers.

The home page uses a curated menu in `app.py` for button grouping, order, shortened labels, and concise descriptions. Add new query modules to that menu when they should appear in a specific home-page section; otherwise they are still auto-discovered and appear under Other Modules.

---

## Adding a new module

1. Copy a similar module.
2. Change `META["key"]`, `META["name"]`, `META["description"]`, and `HEADERS`.
3. Build a `render_fields(form)` function for the needed inputs.
4. Parse and validate form fields defensively.
5. Use `connect_ro()` and parameterized SQL.
6. Return rows in the same order as `HEADERS`.
7. Start the app if needed. If it is already running, refresh the browser page; the query registry reloads automatically when files in `queries/` change.

Minimal skeleton:

```python
from typing import Iterable, Tuple, List
from common import connect_ro, normalize_eins

META = {
    "key": "example_query",
    "name": "Example Query",
    "description": "Describe what this query returns.",
}

HEADERS = ["ein", "org_name", "tax_year"]
META["headers"] = HEADERS

def render_fields(form) -> str:
    val = (form or {}).get("ein_list", "")
    return f"""
    <label><b>EIN(s):</b></label><br>
    <textarea name="ein_list" rows="5">{val}</textarea>
    """

def _query(eins: List[str]):
    if not eins:
        return []
    conn = connect_ro()
    placeholders = ",".join("?" for _ in eins)
    sql = f"""
    SELECT c.ein, r.org_name, c.tax_year
    FROM canonical_by_ein_year c
    JOIN returns r ON r.filing_id = c.filing_id
    WHERE c.ein IN ({placeholders})
    ORDER BY c.ein, c.tax_year DESC
    """
    return conn.execute(sql, eins).fetchall()

def run(form):
    eins = normalize_eins((form or {}).get("ein_list", ""))
    return HEADERS, _query(eins)

def export_rows(form) -> Iterable[Tuple]:
    eins = normalize_eins((form or {}).get("ein_list", ""))
    return _query(eins)
```

---

## Schema guide for module authors

The most commonly used tables/views are:

| Object | Use |
|---|---|
| `returns` | Filing identity, EIN, organization name, DBA, state/city/ZIP, return type, period end, website. |
| `canonical_by_ein_year` | One canonical filing per EIN and tax year. |
| `core_hot` | Core financial summary fields, mission text, employees/volunteers, grants paid, government grants, lobbying expense. |
| `grants_compat_v1` | Normalized grant rows; filer is grantor, row is recipient/grantee. |
| `vw_contractors` | Normalized contractor/vendor rows. |
| `officers` | Officer/director/trustee/key employee rows. |
| `highest_comp_employees` | Highest compensated employees. |
| `former_key_people` | Former/key people. |
| `return_header_all` | Return signer, preparer, preparer firm, PTIN, tax period begin/end. |
| `sched_r_related_orgs_expanded` | Expanded Schedule R related organizations and transactions. |

The detailed model-facing SQL guide is in `ai/irs990_ai_schema.md`.
