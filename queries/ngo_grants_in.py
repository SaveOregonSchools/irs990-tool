# queries/ngo_grants_in.py
# Lists grants RECEIVED by orgs.
# Uses: canonical_by_ein_year, returns, grants_compat_v1
#
# Behavior:
# - If "Return all Non-Profits" is checked, EIN list is ignored.
#   Optionally filter by RECIPIENT state and/or a tax-year range.
# - Otherwise, we consider only the provided EINs (matched to g.recipient_ein).
# - Shows ALL filings/years with grants (no “latest-only” compression).

from typing import List, Tuple, Iterable, Optional
from common import connect_ro, normalize_eins

META = {
    "key": "ngo_grants_in",
    "name": "NGO Grants Received (all filings with grants)",
    "description": (
        "Enter one or more EINs (comma/semicolon/space/newline separated), "
        "or check 'Return all Non-Profits' to ignore EINs. "
        "Optionally filter by RECIPIENT state (2-letter code) and/or a tax-year range. "
        "Lists every grant received by the recipient EINs (from grants_compat_v1), "
        "with grantmaker (filer) context and filing year."
    ),
}

HEADERS = [
    # Recipient (grantee)
    "recipient_ein", "recipient_name",
    "recipient_city", "recipient_state", "recipient_country",
    # Grantmaker (filer)
    "grantor_ein", "grantor_org_name", "grantor_dba_name",
    "grantor_city", "grantor_state", "grantor_zip",
    # Filing context
    "tax_year", "return_type", "period_end", "filing_id",
    # Amounts and purpose
    "cash_amount", "noncash_amount", "total_amount",
    "purpose",
]
META["headers"] = HEADERS

US_STATES = [
    "", "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
    "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]

def _val_js(val: str) -> str:
    return repr(val)

def render_fields(form) -> str:
    f = form or {}
    val_eins = f.get("ein_list", "")
    return_all = "checked" if f.get("return_all") in (True, "true", "on", "1") else ""
    state_val = (f.get("state") or "").upper()
    min_year = str(f.get("min_year") or "")
    max_year = str(f.get("max_year") or "")

    state_options = ["<option value=\"\">(All recipient states)</option>"]
    for s in US_STATES:
        if not s:
            continue
        sel = " selected" if s == state_val else ""
        state_options.append(f"<option value=\"{s}\"{sel}>{s}</option>")
    state_html = "\n".join(state_options)

    return f"""
    <div class="row" style="display:flex; gap:16px; flex-wrap:wrap;">
      <div style="flex:1 1 320px;">
        <label><input type="checkbox" id="return_all" name="return_all" {return_all}>
          <b>Return all Non-Profits</b>
        </label>
        <div style="color:#666; font-size: 90%; margin-top:4px;">
          If checked, the EIN list is ignored; results are optionally filtered by <b>recipient</b> state and/or tax year.
        </div>
      </div>
      <div style="flex:0 1 220px;">
        <label for="state"><b>Recipient state filter</b> (2-letter):</label><br>
        <select id="state" name="state" style="min-width:200px;">{state_html}</select>
        <div style="color:#666; font-size: 90%; margin-top:4px;">
          Leave blank for all states.
        </div>
      </div>
      <div style="flex:0 1 160px;">
        <label for="min_year"><b>Min tax year</b>:</label><br>
        <input id="min_year" name="min_year" type="number" inputmode="numeric" value="{min_year}" placeholder="e.g. 2019" style="width:140px;">
      </div>
      <div style="flex:0 1 160px;">
        <label for="max_year"><b>Max tax year</b>:</label><br>
        <input id="max_year" name="max_year" type="number" inputmode="numeric" value="{max_year}" placeholder="e.g. 2023" style="width:140px;">
      </div>
    </div>

    <div class="row" style="margin-top:12px;">
      <label for="ein_list"><b>Recipient EIN(s):</b></label><br>
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

_SQL_BASE = """
WITH gsrc AS (
  SELECT
    g.filing_id,
    g.recipient_ein,
    g.recipient_name,
    g.city    AS recipient_city,
    g.state   AS recipient_state,
    g.country AS recipient_country,
    g.cash_amount,
    g.noncash_amount,
    g.purpose
  FROM grants_compat_v1 g
  {g_where_clause}
)
SELECT
  -- Recipient
  gsrc.recipient_ein         AS recipient_ein,
  gsrc.recipient_name        AS recipient_name,
  gsrc.recipient_city        AS recipient_city,
  gsrc.recipient_state       AS recipient_state,
  gsrc.recipient_country     AS recipient_country,

  -- Grantmaker (filer)
  rf.ein                     AS grantor_ein,
  rf.org_name                AS grantor_org_name,
  rf.dba_name                AS grantor_dba_name,
  rf.city                    AS grantor_city,
  rf.state                   AS grantor_state,
  rf.zip                     AS grantor_zip,

  -- Filing context
  c.tax_year                 AS tax_year,
  c.return_type              AS return_type,
  c.period_end               AS period_end,
  c.filing_id                AS filing_id,

  -- Amounts & purpose
  gsrc.cash_amount           AS cash_amount,
  gsrc.noncash_amount        AS noncash_amount,
  (COALESCE(gsrc.cash_amount,0) + COALESCE(gsrc.noncash_amount,0)) AS total_amount,
  gsrc.purpose               AS purpose

FROM gsrc
JOIN canonical_by_ein_year c ON c.filing_id = gsrc.filing_id
JOIN returns rf              ON rf.filing_id = c.filing_id
{c_where_clause}
ORDER BY recipient_ein, c.tax_year DESC, total_amount DESC, grantor_org_name, grantor_ein
"""

def _parse_eins(form) -> List[str]:
    text = (form or {}).get("ein_list", "")
    return normalize_eins(text)

def _parse_filters(form) -> Tuple[bool, Optional[str], Optional[int], Optional[int]]:
    f = form or {}
    return_all = f.get("return_all") in (True, "true", "on", "1")
    state = (f.get("state") or "").strip().upper()
    if state and state not in US_STATES:
        state = None

    def _to_int(x):
        try:
            return int(x)
        except Exception:
            return None

    min_year = _to_int(f.get("min_year"))
    max_year = _to_int(f.get("max_year"))
    if min_year and not max_year:
        max_year = min_year
    elif max_year and not min_year:
        min_year = max_year
    if min_year and max_year and min_year > max_year:
        min_year, max_year = max_year, min_year

    return return_all, (state if state else None), min_year, max_year

def _build_where(return_all: bool, eins: List[str], state: Optional[str],
                 min_year: Optional[int], max_year: Optional[int]):
    g_clauses = []
    g_params: List = []

    c_clauses = []
    c_params: List = []

    if not return_all:
        if not eins:
            return "WHERE 1=0", [], "", []
        placeholders = ",".join("?" for _ in eins)
        g_clauses.append(f"g.recipient_ein IN ({placeholders})")
        g_params.extend(eins)

    if state:
        g_clauses.append("g.state = ?")
        g_params.append(state)

    if (min_year is not None) and (max_year is not None):
        c_clauses.append("c.tax_year BETWEEN ? AND ?")
        c_params.extend([min_year, max_year])

    g_where = ("WHERE " + " AND ".join(g_clauses)) if g_clauses else ""
    c_where = ("WHERE " + " AND ".join(c_clauses)) if c_clauses else ""
    return g_where, g_params, c_where, c_params

def _query(eins: List[str], return_all: bool, state: Optional[str],
           min_year: Optional[int], max_year: Optional[int]) -> List[Tuple]:
    g_where, g_params, c_where, c_params = _build_where(return_all, eins, state, min_year, max_year)
    sql = _SQL_BASE.format(g_where_clause=g_where, c_where_clause=c_where)

    conn = connect_ro()
    cur = conn.execute(sql, g_params + c_params)
    return cur.fetchall()

def run(form):
    eins = _parse_eins(form)
    return_all, state, min_year, max_year = _parse_filters(form)
    rows = _query(eins, return_all, state, min_year, max_year)
    return HEADERS, rows

def export_rows(form) -> Iterable[Tuple]:
    eins = _parse_eins(form)
    return_all, state, min_year, max_year = _parse_filters(form)
    return _query(eins, return_all, state, min_year, max_year)