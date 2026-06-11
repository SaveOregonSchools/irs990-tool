# IRS 990 AI Schema Guide

Generate one SQLite query for the IRS 990 research database.

## Output rules

- Return SQL only.
- Use SQLite syntax.
- Generate exactly one query.
- Query must start with `SELECT` or `WITH`.
- Use only the approved tables/views listed below.
- Use only the columns listed for those tables/views.
- Always include a numeric `LIMIT`, normally `LIMIT 500`.
- Do not use: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `ATTACH`, `DETACH`, `PRAGMA`, `VACUUM`, `REPLACE`, `TRUNCATE`, `REINDEX`, transactions, or multiple statements.
- Prefer clear aliases and readable output columns.
- Use `COALESCE(x,0)` when adding nullable money fields.
- Use `NULLIF(x,0)` when dividing by a value that could be zero.
- For name searches, use `UPPER(column) LIKE '%NAME%'`.
- For state filters, use 2-letter uppercase state codes, e.g. `r.state = 'OR'`.
- Do not invent columns such as `revenue`, `year`, `name`, `expenses`, `assets`, or `grant_amount`. Use the exact approved column names.

## Core join pattern

Use this for most filing/financial questions:

```sql
FROM canonical_by_ein_year c
JOIN returns r ON r.filing_id = c.filing_id
LEFT JOIN core_hot h ON h.filing_id = c.filing_id
```

`canonical_by_ein_year` gives one canonical filing per EIN per tax year. Prefer it when filtering by year or comparing years.

## Approved tables/views

### returns r

One row per filing.

Columns:
`filing_id`, `ein`, `org_name`, `dba_name`, `return_type`, `tax_year`, `period_end`, `city`, `state`, `zip`, `website`

Use for:
organization identity, filer EIN, filer name, filer state/city, filing type.

### canonical_by_ein_year c

One canonical filing per EIN per tax year.

Columns:
`ein`, `tax_year`, `filing_id`, `return_type`, `return_ts`, `amended_return_ind`, `period_end`

Join:
`c.filing_id = r.filing_id`

Use for:
year filtering, latest/canonical filing selection, return type, period end.

### core_hot h

Financial summary fields.

Columns:
`filing_id`, `total_revenue`, `total_expenses`, `net_assets_boy`, `net_assets_eoy`, `contributions`, `program_service_revenue`, `membership_dues`, `investment_income`, `government_grants`, `grants_paid`, `lobbying_expense`, `employees_count`, `volunteers_count`, `mission_desc`

Join:
`h.filing_id = r.filing_id`

Use for:
revenue, expenses, assets, government grants, lobbying expense, grants paid totals, employees, volunteers, mission text.

### grants_compat_v1 g

Normalized grant rows.

Columns:
`filing_id`, `recipient_ein`, `recipient_name`, `city`, `state`, `country`, `cash_amount`, `noncash_amount`, `purpose`

Join:
`g.filing_id = r.filing_id`

Meaning:
The filer in `returns` is the grantor/funder. The row in `grants_compat_v1` is the grantee/recipient.

Use for:
grants paid by a filer, grants received by a recipient, grant purpose, recipient state/country.

For total grant amount:

```sql
COALESCE(g.cash_amount,0) + COALESCE(g.noncash_amount,0)
```

### vw_contractors vc

Normalized contractor rows.

Columns:
`filing_id`, `contractor_name`, `business_name_line1_txt`, `business_name_line2_txt`, `person_nm`, `services_desc`, `compensation_amt`, `city`, `region`, `country`, `is_us_address`

Join:
`vc.filing_id = r.filing_id`

Use for:
contractors/vendors paid by nonprofits, services, contractor compensation, contractor location.

### officers o

Officer/director/trustee/key employee rows.

Columns:
`filing_id`, `person_name`, `title_txt`, `avg_hours_week`, `comp_from_org`, `comp_from_related`, `other_compensation`, `is_officer`, `is_director`, `is_key_employee`, `is_former`

Join:
`o.filing_id = r.filing_id`

Use for:
board/officer lookups, titles, compensation, identifying officers/directors/key employees.

### highest_comp_employees he

Highest compensated employee rows.

Columns:
`filing_id`, `person_name`, `title_txt`, `avg_hours_week`, `comp_from_org`, `comp_from_related`, `other_compensation`

Join:
`he.filing_id = r.filing_id`

Use for:
highest paid employees who may not be officers.

### former_key_people fk

Former/key people rows.

Columns:
`filing_id`, `person_name`, `title_txt`, `comp_from_org`, `comp_from_related`, `other_compensation`

Join:
`fk.filing_id = r.filing_id`

Use for:
former/key employee searches.

### return_header_all rh

Return signer/preparer data.

Columns:
`filing_id`, `person_nm`, `preparer_person_nm`, `person_title_txt`, `signature_dt`, `preparer_firm_name_business_name_line1_txt`, `ptin`, `preparation_dt`, `tax_period_begin_dt`, `tax_period_end_dt`

Join:
`rh.filing_id = r.filing_id`

Use for:
return signer, preparer, preparer firm, tax period begin/end.

### sched_r_related_orgs_expanded sr

Schedule R related organizations.

Columns:
`filing_id`, `relationship_category`, `related_ein`, `related_name_line1`, `related_name_line2`, `ownership_pct`, `controlled_organization_ind`, `primary_activities_txt`, `transaction_type_txt`, `involved_amt`, `exempt_code_section_txt`, `public_charity_status_txt`, `city_nm`, `state_abbreviation_cd`, `legal_domicile_state_cd`, `country_cd`

Join:
`sr.filing_id = r.filing_id`

Use for:
related organizations, controlled organizations, disregarded entities, related taxable partnerships/corps/trusts, Schedule R transactions.

For related org display name:

```sql
TRIM(COALESCE(sr.related_name_line1,'') ||
     CASE WHEN COALESCE(sr.related_name_line2,'') <> '' THEN ' ' || sr.related_name_line2 ELSE '' END)
```

## Calculation rules

Use SQLite arithmetic directly. Do not invent calculated columns.

Common calculations:

- Revenue growth amount: `revenue_later - revenue_earlier`
- Revenue growth percent: `100.0 * (revenue_later - revenue_earlier) / NULLIF(revenue_earlier,0)`
- Expense growth amount: `expenses_later - expenses_earlier`
- Expense growth percent: `100.0 * (expenses_later - expenses_earlier) / NULLIF(expenses_earlier,0)`
- Net asset growth amount: `net_assets_later - net_assets_earlier`
- Grant total: `COALESCE(g.cash_amount,0) + COALESCE(g.noncash_amount,0)`

When calculating growth between two years, create one CTE for the earlier year and one CTE for the later year, then join them by EIN. This avoids mixing rows from different years.

When ranking growth, usually order by the amount of growth unless the user specifically asks for percentage growth.

When calculating percentage growth, exclude or protect zero/blank starting values with `WHERE earlier_value > 0` or `NULLIF(earlier_value,0)`.

## Common query patterns

### Core financial filters

Use `returns` + `canonical_by_ein_year` + `core_hot`.

Example:

```sql
SELECT
  r.ein,
  r.org_name,
  r.state,
  c.tax_year,
  h.government_grants,
  h.total_revenue
FROM canonical_by_ein_year c
JOIN returns r ON r.filing_id = c.filing_id
JOIN core_hot h ON h.filing_id = c.filing_id
WHERE r.state = 'OR'
  AND c.tax_year = 2022
  AND h.government_grants > 1000000
ORDER BY h.government_grants DESC
LIMIT 500;
```

### Multi-year revenue growth

Use this pattern for questions like “top nonprofits by revenue growth from 2018 to 2024.”

```sql
WITH rev_2018 AS (
  SELECT
    c.ein,
    r.org_name,
    r.state,
    h.total_revenue AS revenue_2018
  FROM canonical_by_ein_year c
  JOIN returns r ON r.filing_id = c.filing_id
  JOIN core_hot h ON h.filing_id = c.filing_id
  WHERE c.tax_year = 2018
    AND h.total_revenue IS NOT NULL
),
rev_2024 AS (
  SELECT
    c.ein,
    r.org_name,
    r.state,
    h.total_revenue AS revenue_2024
  FROM canonical_by_ein_year c
  JOIN returns r ON r.filing_id = c.filing_id
  JOIN core_hot h ON h.filing_id = c.filing_id
  WHERE c.tax_year = 2024
    AND h.total_revenue IS NOT NULL
)
SELECT
  r24.ein,
  r24.org_name,
  r24.state,
  r18.revenue_2018,
  r24.revenue_2024,
  r24.revenue_2024 - r18.revenue_2018 AS revenue_growth_amount,
  ROUND(100.0 * (r24.revenue_2024 - r18.revenue_2018) / NULLIF(r18.revenue_2018,0), 2) AS revenue_growth_pct
FROM rev_2024 r24
JOIN rev_2018 r18 ON r18.ein = r24.ein
WHERE r18.revenue_2018 > 0
ORDER BY revenue_growth_amount DESC
LIMIT 25;
```

### Multi-year financial comparison for several fields

Use this pattern when comparing revenue, expenses, grants, assets, employees, or other fields from `core_hot`.

```sql
WITH y1 AS (
  SELECT
    c.ein,
    r.org_name,
    r.state,
    h.total_revenue,
    h.total_expenses,
    h.government_grants,
    h.grants_paid,
    h.net_assets_eoy,
    h.employees_count
  FROM canonical_by_ein_year c
  JOIN returns r ON r.filing_id = c.filing_id
  JOIN core_hot h ON h.filing_id = c.filing_id
  WHERE c.tax_year = 2018
),
y2 AS (
  SELECT
    c.ein,
    r.org_name,
    r.state,
    h.total_revenue,
    h.total_expenses,
    h.government_grants,
    h.grants_paid,
    h.net_assets_eoy,
    h.employees_count
  FROM canonical_by_ein_year c
  JOIN returns r ON r.filing_id = c.filing_id
  JOIN core_hot h ON h.filing_id = c.filing_id
  WHERE c.tax_year = 2024
)
SELECT
  y2.ein,
  y2.org_name,
  y2.state,
  y1.total_revenue AS revenue_2018,
  y2.total_revenue AS revenue_2024,
  y2.total_revenue - y1.total_revenue AS revenue_change,
  y1.total_expenses AS expenses_2018,
  y2.total_expenses AS expenses_2024,
  y2.total_expenses - y1.total_expenses AS expenses_change,
  y1.government_grants AS government_grants_2018,
  y2.government_grants AS government_grants_2024,
  y2.government_grants - y1.government_grants AS government_grants_change
FROM y2
JOIN y1 ON y1.ein = y2.ein
ORDER BY revenue_change DESC
LIMIT 500;
```

### Year-over-year trend for one organization

Use this pattern when the user asks for revenue, expenses, grants, assets, or employees over time.

```sql
SELECT
  c.ein,
  r.org_name,
  c.tax_year,
  h.total_revenue,
  h.total_expenses,
  h.government_grants,
  h.grants_paid,
  h.net_assets_eoy,
  h.employees_count
FROM canonical_by_ein_year c
JOIN returns r ON r.filing_id = c.filing_id
JOIN core_hot h ON h.filing_id = c.filing_id
WHERE UPPER(r.org_name) LIKE '%FOUNDATION%'
ORDER BY c.tax_year, r.org_name
LIMIT 500;
```

### Latest filing per EIN

Use this pattern when the user asks for the latest or most recent filing for each organization.

```sql
WITH ranked AS (
  SELECT
    c.ein,
    r.org_name,
    r.state,
    c.tax_year,
    c.return_type,
    c.period_end,
    h.total_revenue,
    ROW_NUMBER() OVER (
      PARTITION BY c.ein
      ORDER BY c.tax_year DESC, c.return_ts DESC, c.filing_id DESC
    ) AS rn
  FROM canonical_by_ein_year c
  JOIN returns r ON r.filing_id = c.filing_id
  LEFT JOIN core_hot h ON h.filing_id = c.filing_id
)
SELECT
  ein,
  org_name,
  state,
  tax_year,
  return_type,
  period_end,
  total_revenue
FROM ranked
WHERE rn = 1
ORDER BY org_name
LIMIT 500;
```

### Grants paid by an organization

Search the filer/grantor in `returns`.

```sql
SELECT
  r.ein AS grantor_ein,
  r.org_name AS grantor_name,
  c.tax_year,
  g.recipient_ein,
  g.recipient_name,
  g.state AS recipient_state,
  COALESCE(g.cash_amount,0) + COALESCE(g.noncash_amount,0) AS total_amount,
  g.purpose
FROM canonical_by_ein_year c
JOIN returns r ON r.filing_id = c.filing_id
JOIN grants_compat_v1 g ON g.filing_id = c.filing_id
WHERE UPPER(r.org_name) LIKE '%THE CONTINGENT%'
ORDER BY c.tax_year DESC, total_amount DESC
LIMIT 500;
```

### Grants received by an organization

Search the recipient/grantee in `grants_compat_v1`.

```sql
SELECT
  g.recipient_ein,
  g.recipient_name,
  g.state AS recipient_state,
  r.ein AS grantor_ein,
  r.org_name AS grantor_name,
  c.tax_year,
  COALESCE(g.cash_amount,0) + COALESCE(g.noncash_amount,0) AS total_amount,
  g.purpose
FROM grants_compat_v1 g
JOIN canonical_by_ein_year c ON c.filing_id = g.filing_id
JOIN returns r ON r.filing_id = g.filing_id
WHERE UPPER(g.recipient_name) LIKE '%OREGON%'
ORDER BY total_amount DESC
LIMIT 500;
```

### Contractors paid by nonprofits

```sql
SELECT
  r.ein,
  r.org_name,
  r.state,
  c.tax_year,
  vc.contractor_name,
  vc.services_desc,
  vc.compensation_amt,
  vc.city,
  vc.region,
  vc.country
FROM canonical_by_ein_year c
JOIN returns r ON r.filing_id = c.filing_id
JOIN vw_contractors vc ON vc.filing_id = c.filing_id
WHERE r.state = 'OR'
  AND c.tax_year = 2023
ORDER BY vc.compensation_amt DESC
LIMIT 500;
```

### People/officers

```sql
SELECT
  r.ein,
  r.org_name,
  c.tax_year,
  o.person_name,
  o.title_txt,
  o.comp_from_org,
  o.comp_from_related,
  o.other_compensation
FROM canonical_by_ein_year c
JOIN returns r ON r.filing_id = c.filing_id
JOIN officers o ON o.filing_id = c.filing_id
WHERE UPPER(o.person_name) LIKE '%JANE DOE%'
ORDER BY c.tax_year DESC, r.org_name
LIMIT 500;
```

### Related organizations

```sql
SELECT
  r.ein AS filer_ein,
  r.org_name AS filer_name,
  c.tax_year,
  sr.relationship_category,
  sr.related_ein,
  TRIM(COALESCE(sr.related_name_line1,'') ||
       CASE WHEN COALESCE(sr.related_name_line2,'') <> '' THEN ' ' || sr.related_name_line2 ELSE '' END) AS related_name,
  sr.ownership_pct,
  sr.controlled_organization_ind,
  sr.primary_activities_txt
FROM canonical_by_ein_year c
JOIN returns r ON r.filing_id = c.filing_id
JOIN sched_r_related_orgs_expanded sr ON sr.filing_id = c.filing_id
WHERE UPPER(r.org_name) LIKE '%FOUNDATION%'
ORDER BY c.tax_year DESC, related_name
LIMIT 500;
```

## Interpretation notes

- “Nonprofit,” “organization,” “filer,” or “grantor” usually means `returns`.
- “Recipient,” “grantee,” or “grant received” usually means `grants_compat_v1`.
- “Revenue” usually means `core_hot.total_revenue`.
- “Expenses” usually means `core_hot.total_expenses`.
- “Net assets” usually means `core_hot.net_assets_eoy` for end-of-year net assets.
- “Beginning net assets” means `core_hot.net_assets_boy`.
- “Government grants” usually means `core_hot.government_grants`.
- “Grants paid” as a financial total usually means `core_hot.grants_paid`.
- Individual grant rows use `grants_compat_v1`.
- “Lobbying expense” usually means `core_hot.lobbying_expense`.
- “Mission” means `core_hot.mission_desc`.
- “Contractor,” “vendor,” or “services” usually means `vw_contractors`.
- “Related org,” “controlled org,” “Schedule R,” or “disregarded entity” usually means `sched_r_related_orgs_expanded`.
- For years, prefer `canonical_by_ein_year.tax_year`.
- For filer state, use `returns.state`.
- For recipient state, use `grants_compat_v1.state`.
- For return type, use `canonical_by_ein_year.return_type` unless the query is only using `returns`.
- For period end, use `canonical_by_ein_year.period_end` unless the query is only using `returns`.

## Avoid these mistakes

- Do not use `r.name`; use `r.org_name`.
- Do not use `r.year`; use `c.tax_year`.
- Do not use `r.revenue`; use `h.total_revenue`.
- Do not use `h.revenue`; use `h.total_revenue`.
- Do not use `h.expenses`; use `h.total_expenses`.
- Do not use `h.assets`; use `h.net_assets_eoy`, `h.net_assets_boy`, or approved asset fields only if available.
- Do not use `g.amount`; use `COALESCE(g.cash_amount,0) + COALESCE(g.noncash_amount,0)`.
- Do not join two years by organization name. Join years by EIN.
- Do not compare two years in the same row. Use separate CTEs or aliases for each year.
- Do not order by a column alias that was not selected or defined.
