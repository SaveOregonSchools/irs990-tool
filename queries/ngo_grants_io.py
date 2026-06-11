# queries/ngo_grants_io.py
# Combined "grants paid" + "grants received" with:
#  - UI max rows (default 1,048,000), cap enforced across chunks
#  - Rowcount summary exposed via META["post_query_html"]
#  - latest_filer_org_name (most recent org_name for the filer EIN in result scope)
#  - NEW: "Remove duplicate rows" toggle (ON by default). Dedupe keeps first occurrence.
#
# Uses: canonical_by_ein_year, returns, grants_compat_v1
# Aligns with separate "in" and "out" modules' semantics.  (See your originals)
#
from typing import List, Tuple, Iterable, Optional
from collections import OrderedDict
from common import connect_ro, normalize_eins

META = {
    "key": "ngo_grants_io",
    "name": "NGO Grants Paid/Received (combined, with dedupe)",
    "description": (
        "Enter EINs (comma/semicolon/space/newline), choose Paid / Received / Both, "
        "or check 'Return all Non-Profits' to ignore EINs. Filters: filer state (Paid), "
        "recipient state (Received), tax-year range, Max rows, and an option to remove duplicate rows. "
        "Adds latest_filer_org_name (from the filer's most-recent filing within result scope) and shows row count."
    ),
}

HEADERS = [
    # Filer (grant maker) — outs order, with latest_filer_org_name after filer_org_name
    "filer_ein", "filer_org_name", "latest_filer_org_name", "filer_dba_name",
    "filer_city", "filer_state", "filer_zip",
    "tax_year", "return_type", "period_end", "filing_id",
    # Recipient (grantee)
    "recipient_ein", "recipient_name",
    "recipient_city", "recipient_state", "recipient_country",
    # Amounts & purpose
    "cash_amount", "noncash_amount", "total_amount",
    "purpose",
]
META["headers"] = HEADERS

US_STATES = [
    "", "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
    "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]

DEFAULT_MAX_ROWS = 1_048_000

def _val_js(val: str) -> str:
    return repr(val)

def render_fields(form) -> str:
    f = form or {}
    val_eins = f.get("ein_list", "")
    return_all = "checked" if f.get("return_all") in (True, "true", "on", "1") else ""

    # Mode
    mode = (f.get("mode") or "both").lower()
    def _sel(x): return " selected" if mode == x else ""

    # Filters
    filer_state_val = (f.get("filer_state") or "").upper()
    recipient_state_val = (f.get("recipient_state") or "").upper()
    min_year = str(f.get("min_year") or "")
    max_year = str(f.get("max_year") or "")

    # Max rows
    max_rows_val = str(f.get("max_rows") or DEFAULT_MAX_ROWS)

    # Dedupe toggle
    dedupe_checked = "checked" if f.get("dedupe") not in ("false", "", None) else ""  # default ON

    def _state_options(selected: str) -> str:
        opts = ['<option value="">(All)</option>']
        for s in US_STATES:
            if not s: continue
            sel = " selected" if s == selected else ""
            opts.append(f'<option value="{s}"{sel}>{s}</option>')
        return "\n".join(opts)

    return f"""
    <div class="row" style="display:flex; gap:16px; flex-wrap:wrap;">
      <div style="flex:1 1 320px;">
        <label><input type="checkbox" id="return_all" name="return_all" {return_all}>
          <b>Return all Non-Profits</b>
        </label>
        <div style="color:#666; font-size: 90%; margin-top:4px;">
          If checked, the EIN list is ignored. Use the state/year filters below.
        </div>
      </div>

      <div style="flex:0 1 220px;">
        <label for="mode"><b>Show grants</b>:</label><br>
        <select id="mode" name="mode" style="min-width:200px;">
          <option value="paid"{_sel("paid")}>Paid (grants issued by filer EINs)</option>
          <option value="received"{_sel("received")}>Received (grants received by recipient EINs)</option>
          <option value="both"{_sel("both")}>Both</option>
        </select>
        <div style="color:#666; font-size: 90%; margin-top:4px;">Applies to how EINs are matched.</div>
      </div>

      <div style="flex:0 1 180px;">
        <label for="filer_state"><b>Filer state</b> (Paid mode):</label><br>
        <select id="filer_state" name="filer_state" style="min-width:160px;">{_state_options(filer_state_val)}</select>
      </div>

      <div style="flex:0 1 200px;">
        <label for="recipient_state"><b>Recipient state</b> (Received mode):</label><br>
        <select id="recipient_state" name="recipient_state" style="min-width:180px;">{_state_options(recipient_state_val)}</select>
      </div>

      <div style="flex:0 1 140px;">
        <label for="min_year"><b>Min tax year</b>:</label><br>
        <input id="min_year" name="min_year" type="number" inputmode="numeric" value="{min_year}" placeholder="e.g. 2019" style="width:120px;">
      </div>

      <div style="flex:0 1 140px;">
        <label for="max_year"><b>Max tax year</b>:</label><br>
        <input id="max_year" name="max_year" type="number" inputmode="numeric" value="{max_year}" placeholder="e.g. 2023" style="width:120px;">
      </div>

      <div style="flex:0 1 180px;">
        <label for="max_rows"><b>Max rows</b>:</label><br>
        <input id="max_rows" name="max_rows" type="number" inputmode="numeric" value="{max_rows_val}" style="width:160px;">
        <div style="color:#666; font-size: 90%; margin-top:4px;">Default {DEFAULT_MAX_ROWS:,} (Excel-ish limit).</div>
      </div>

      <div style="flex:0 1 240px; align-self:flex-end;">
        <label><input type="checkbox" id="dedupe" name="dedupe" {dedupe_checked}>
          <b>Remove duplicate rows</b> (default)
        </label>
      </div>
    </div>

    <div class="row" style="margin-top:12px;">
      <label for="ein_list"><b>EIN(s):</b></label><br>
      <textarea id="ein_list" name="ein_list" rows="6" placeholder="e.g. 131624102, 941156365; 52-6043385
123456789"></textarea>
      <script>
        (function() {{
          var el = document.getElementById('ein_list');
          el.value = {_val_js(val_eins)};
          function toggleEins() {{
            var checked = document.getElementById('return_all').checked;
            el.disabled = checked;
            el.style.backgroundColor = checked ? '#f3f3f3' : 'white';
          }}
          document.getElementById('return_all').addEventListener('change', toggleEins);
          toggleEins();
        }})();
      </script>
      <div style="color:#666; font-size: 90%; margin-top:4px;">
        Separate by commas, semicolons, spaces, or new lines. Non-digits ignored; valid 9-digit EINs are kept.
      </div>
    </div>
    """

def _parse_eins(form) -> List[str]:
    text = (form or {}).get("ein_list", "")
    return normalize_eins(text)

def _to_int(x):
    try:
        return int(str(x).strip())
    except Exception:
        return None

def _parse_filters(form):
    f = form or {}
    return_all = f.get("return_all") in (True, "true", "on", "1")
    mode = (f.get("mode") or "both").lower()
    if mode not in ("paid", "received", "both"):
        mode = "both"

    filer_state = (f.get("filer_state") or "").strip().upper()
    recipient_state = (f.get("recipient_state") or "").strip().upper()
    if filer_state and filer_state not in US_STATES: filer_state = None
    if recipient_state and recipient_state not in US_STATES: recipient_state = None

    min_year = _to_int(f.get("min_year"))
    max_year = _to_int(f.get("max_year"))
    if min_year and not max_year:
        max_year = min_year
    elif max_year and not min_year:
        min_year = max_year
    if min_year and max_year and min_year > max_year:
        min_year, max_year = max_year, min_year

    max_rows = _to_int(f.get("max_rows")) or DEFAULT_MAX_ROWS
    if max_rows <= 0:
        max_rows = DEFAULT_MAX_ROWS

    dedupe = f.get("dedupe") not in ("false", "", None)  # default True
    return return_all, mode, (filer_state or None), (recipient_state or None), min_year, max_year, max_rows, dedupe

# ---- SQL templates ----
# For performance: compute latest org_name only for EINs that actually appear in the current result.
_SQL_PAID = """
WITH candidates AS (
  SELECT c.ein, c.tax_year, c.filing_id, c.return_type, c.period_end, c.return_ts
  FROM canonical_by_ein_year c
  JOIN returns r ON r.filing_id = c.filing_id
  {where_clause}
),
candidates_with_grants AS (
  SELECT DISTINCT c.*
  FROM candidates c
  JOIN grants_compat_v1 g ON g.filing_id = c.filing_id
),
ein_pool AS (
  SELECT DISTINCT rf.ein AS ein
  FROM candidates_with_grants c
  JOIN returns rf ON rf.filing_id = c.filing_id
),
latest_names AS (
  SELECT ln_ein, ln_org_name FROM (
    SELECT
      c2.ein AS ln_ein,
      rf2.org_name AS ln_org_name,
      ROW_NUMBER() OVER (PARTITION BY c2.ein ORDER BY c2.tax_year DESC, c2.return_ts DESC) AS rn
    FROM canonical_by_ein_year c2
    JOIN returns rf2 ON rf2.filing_id = c2.filing_id
    JOIN ein_pool ep ON ep.ein = c2.ein
  ) t WHERE rn = 1
)
SELECT
  rf.ein              AS filer_ein,
  rf.org_name         AS filer_org_name,
  ln.ln_org_name      AS latest_filer_org_name,
  rf.dba_name         AS filer_dba_name,
  rf.city             AS filer_city,
  rf.state            AS filer_state,
  rf.zip              AS filer_zip,
  c.tax_year          AS tax_year,
  c.return_type       AS return_type,
  c.period_end        AS period_end,
  c.filing_id         AS filing_id,
  g.recipient_ein     AS recipient_ein,
  g.recipient_name    AS recipient_name,
  g.city              AS recipient_city,
  g.state             AS recipient_state,
  g.country           AS recipient_country,
  g.cash_amount       AS cash_amount,
  g.noncash_amount    AS noncash_amount,
  (COALESCE(g.cash_amount,0) + COALESCE(g.noncash_amount,0)) AS total_amount,
  g.purpose           AS purpose
FROM candidates_with_grants c
JOIN returns rf         ON rf.filing_id = c.filing_id
JOIN grants_compat_v1 g ON g.filing_id = c.filing_id
JOIN latest_names ln    ON ln.ln_ein    = rf.ein
ORDER BY rf.ein, c.tax_year DESC, total_amount DESC, g.recipient_name, g.recipient_ein
"""

_SQL_RECEIVED = """
WITH gsrc AS (
  SELECT
    g.filing_id,
    g.recipient_ein,
    g.recipient_name,
    g.city      AS recipient_city,
    g.state     AS recipient_state,
    g.country   AS recipient_country,
    g.cash_amount,
    g.noncash_amount,
    g.purpose
  FROM grants_compat_v1 g
  {g_where_clause}
),
joined AS (
  SELECT
    g.*,
    c.tax_year,
    c.return_type,
    c.period_end
  FROM gsrc g
  JOIN canonical_by_ein_year c ON c.filing_id = g.filing_id
  {c_where_clause}
),
ein_pool AS (
  SELECT DISTINCT rf.ein AS ein
  FROM joined j
  JOIN returns rf ON rf.filing_id = j.filing_id
),
latest_names AS (
  SELECT ln_ein, ln_org_name FROM (
    SELECT
      c2.ein AS ln_ein,
      rf2.org_name AS ln_org_name,
      ROW_NUMBER() OVER (PARTITION BY c2.ein ORDER BY c2.tax_year DESC, c2.return_ts DESC) AS rn
    FROM canonical_by_ein_year c2
    JOIN returns rf2 ON rf2.filing_id = c2.filing_id
    JOIN ein_pool ep ON ep.ein = c2.ein
  ) t WHERE rn = 1
)
SELECT
  rf.ein              AS filer_ein,
  rf.org_name         AS filer_org_name,
  ln.ln_org_name      AS latest_filer_org_name,
  rf.dba_name         AS filer_dba_name,
  rf.city             AS filer_city,
  rf.state            AS filer_state,
  rf.zip              AS filer_zip,
  j.tax_year          AS tax_year,
  j.return_type       AS return_type,
  j.period_end        AS period_end,
  j.filing_id         AS filing_id,
  j.recipient_ein     AS recipient_ein,
  j.recipient_name    AS recipient_name,
  j.recipient_city    AS recipient_city,
  j.recipient_state   AS recipient_state,
  j.recipient_country AS recipient_country,
  j.cash_amount       AS cash_amount,
  j.noncash_amount    AS noncash_amount,
  (COALESCE(j.cash_amount,0) + COALESCE(j.noncash_amount,0)) AS total_amount,
  j.purpose           AS purpose
FROM joined j
JOIN returns rf      ON rf.filing_id = j.filing_id
JOIN latest_names ln ON ln.ln_ein    = rf.ein
ORDER BY rf.ein, j.tax_year DESC, total_amount DESC, j.recipient_name, j.recipient_ein
"""

def _build_paid_where(return_all: bool, eins: List[str], filer_state: Optional[str],
                      min_year: Optional[int], max_year: Optional[int]):
    clauses, params = [], []
    if not return_all:
        if not eins:
            return "WHERE 1=0", []
        placeholders = ",".join("?" for _ in eins)
        clauses.append(f"c.ein IN ({placeholders})")
        params.extend(eins)
    if filer_state:
        clauses.append("r.state = ?")
        params.append(filer_state)
    if (min_year is not None) and (max_year is not None):
        clauses.append("c.tax_year BETWEEN ? AND ?")
        params.extend([min_year, max_year])
    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params

def _build_received_where(return_all: bool, eins: List[str], recipient_state: Optional[str],
                          min_year: Optional[int], max_year: Optional[int]):
    g_clauses, g_params = [], []
    c_clauses, c_params = [], []
    if not return_all:
        if not eins:
            return "WHERE 1=0", [], "", []
        placeholders = ",".join("?" for _ in eins)
        g_clauses.append(f"g.recipient_ein IN ({placeholders})")
        g_params.extend(eins)
    if recipient_state:
        g_clauses.append("g.state = ?")
        g_params.append(recipient_state)
    if (min_year is not None) and (max_year is not None):
        c_clauses.append("c.tax_year BETWEEN ? AND ?")
        c_params.extend([min_year, max_year])
    g_where = "WHERE " + " AND ".join(g_clauses) if g_clauses else ""
    c_where = "WHERE " + " AND ".join(c_clauses) if c_clauses else ""
    return g_where, g_params, c_where, c_params

def _query(return_all: bool, mode: str, eins: List[str],
           filer_state: Optional[str], recipient_state: Optional[str],
           min_year: Optional[int], max_year: Optional[int],
           max_rows: int, dedupe: bool) -> List[Tuple]:
    conn = connect_ro()
    rows_all: List[Tuple] = []

    def _run_paid(ein_subset: Optional[List[str]], budget: int) -> List[Tuple]:
        where_clause, params = _build_paid_where(
            return_all if ein_subset is None else False,
            [] if ein_subset is None else ein_subset,
            filer_state, min_year, max_year
        )
        sql = _SQL_PAID.format(where_clause=("\n" + where_clause if where_clause else ""))
        cur = conn.execute(sql, params)
        return cur.fetchmany(budget)

    def _run_received(ein_subset: Optional[List[str]], budget: int) -> List[Tuple]:
        g_where, g_params, c_where, c_params = _build_received_where(
            return_all if ein_subset is None else False,
            [] if ein_subset is None else ein_subset,
            recipient_state, min_year, max_year
        )
        sql = _SQL_RECEIVED.format(
            g_where_clause=("\n" + g_where if g_where else ""),
            c_where_clause=("\n" + c_where if c_where else "")
        )
        cur = conn.execute(sql, g_params + c_params)
        return cur.fetchmany(budget)

    remaining = max_rows
    CHUNK = 300

    if mode in ("paid", "both") and remaining > 0:
        if return_all:
            part = _run_paid(None, remaining)
            rows_all.extend(part); remaining -= len(part)
        else:
            for i in range(0, len(eins), CHUNK):
                if remaining <= 0: break
                part = _run_paid(eins[i:i+CHUNK], remaining)
                rows_all.extend(part); remaining -= len(part)

    if mode in ("received", "both") and remaining > 0:
        if return_all:
            part = _run_received(None, remaining)
            rows_all.extend(part); remaining -= len(part)
        else:
            for i in range(0, len(eins), CHUNK):
                if remaining <= 0: break
                part = _run_received(eins[i:i+CHUNK], remaining)
                rows_all.extend(part); remaining -= len(part)

    # Optional duplicate filtering (default ON).
    # We consider the FULL OUTPUT ROW as the identity for duplicates,
    # which safely catches cross-branch duplicates in 'Both' mode.
    final_rows: List[Tuple]
    deduped = False
    if dedupe and rows_all:
        seen = OrderedDict()
        for r in rows_all:
            # "r" is already a tuple matching HEADERS order; use it as a key.
            if r not in seen:
                seen[r] = None
        final_rows = list(seen.keys())
        deduped = len(final_rows) != len(rows_all)
    else:
        final_rows = rows_all

    # If dedupe expanded available budget (rare), keep at most max_rows for display/export consistency
    if len(final_rows) > max_rows:
        final_rows = final_rows[:max_rows]
        capped = True
    else:
        capped = len(rows_all) >= max_rows  # capped by earlier fetchmany budget

    META["post_query_html"] = (
        f"<div style='margin-top:8px;color:#444;'>Returned <b>{len(final_rows):,}</b> row(s)"
        f"{' (deduped)' if deduped else ''}"
        f"{' (capped)' if capped else ''}.</div>"
    )
    META["last_rowcount"] = len(final_rows)
    META["deduped"] = deduped
    META["capped"] = capped

    return final_rows

def run(form) -> Tuple[List[str], List[Tuple]]:
    return_all, mode, filer_state, recipient_state, min_year, max_year, max_rows, dedupe = _parse_filters(form)
    eins = [] if return_all else _parse_eins(form)
    rows = _query(return_all, mode, eins, filer_state, recipient_state, min_year, max_year, max_rows, dedupe)
    return HEADERS, rows

def export_rows(form) -> Iterable[Tuple]:
    return_all, mode, filer_state, recipient_state, min_year, max_year, max_rows, dedupe = _parse_filters(form)
    eins = [] if return_all else _parse_eins(form)
    return _query(return_all, mode, eins, filer_state, recipient_state, min_year, max_year, max_rows, dedupe)
