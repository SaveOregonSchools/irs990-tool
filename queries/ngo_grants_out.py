# queries/ngo_grants_out.py
# Modeled after ngo_core_data_v4, but focused on "grants paid" from the most recent filing per EIN.
# Uses: canonical_by_ein_year, returns, grants
#
# Behavior:
# - If "Return all Non-Profits" is checked, EIN list is ignored and we consider all filers,
#   optionally filtered by filer state and/or tax year range.
# - Otherwise, we consider only the provided EINs.
# - From the resulting candidate (EIN, tax_year) rows, we pick the most recent filing per EIN
#   (max tax_year; tiebreak on return_ts), then list every grant row tied to that filing_id.
#
# Fields referenced (per schema):
# canonical_by_ein_year: ein, tax_year, filing_id, return_type, return_ts, period_end
# returns: filing_id, ein, org_name, dba_name, city, state, zip
# grants_compat_v1: filing_id, recipient_ein, recipient_name, city, state, country, cash_amount, noncash_amount, purpose

from typing import List, Tuple, Iterable, Optional
from common import connect_ro, normalize_eins

META = {
    "key": "ngo_grants_out",
    "name": "NGO Grants Paid (all filings with grants)",
    "description": (
        "Enter one or more EINs (comma/semicolon/space/newline separated), "
        "or check 'Return all Non-Profits' to ignore EINs. "
        "Optionally filter by filer state (2-letter code) and/or a tax-year range. "
        "For each relevant EIN and tax year, lists every grant paid (from the grants_compat_v1 view)."
    ),
}

HEADERS = [
    # Filer (grant maker)
    "filer_ein", "filer_org_name", "filer_dba_name",
    "filer_city", "filer_state", "filer_zip",
    "tax_year", "return_type", "period_end", "filing_id",
    # Recipient (grantee)
    "recipient_ein", "recipient_name",
    "recipient_city", "recipient_state", "recipient_country",
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
    # Safe JS string (basic)
    return repr(val)

def render_fields(form) -> str:
    f = form or {}
    val_eins = f.get("ein_list", "")
    return_all = "checked" if f.get("return_all") in (True, "true", "on", "1") else ""
    state_val = (f.get("state") or "").upper()
    min_year = str(f.get("min_year") or "")
    max_year = str(f.get("max_year") or "")

    state_options = ["<option value=\"\">(All states)</option>"]
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
          If checked, the EIN list is ignored; results are optionally filtered by filer state and/or tax year. For each EIN, the most recent filing is used.
        </div>
      </div>
      <div style="flex:0 1 200px;">
        <label for="state"><b>Filer state filter</b> (2-letter):</label><br>
        <select id="state" name="state" style="min-width:180px;">{state_html}</select>
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

# Build the core SQL using CTEs so we can pick the most recent filing per EIN (within filtered candidates).
_SQL_BASE = """
WITH candidates AS (
  SELECT
    c.ein,
    c.tax_year,
    c.filing_id,
    c.return_type,
    c.return_ts,
    c.period_end
  FROM canonical_by_ein_year c
  JOIN returns r ON r.filing_id = c.filing_id
  {where_clause}
),
candidates_with_grants AS (
  SELECT DISTINCT c.*
  FROM candidates c
  JOIN grants_compat_v1 g ON g.filing_id = c.filing_id
)
SELECT
  rf.ein              AS filer_ein,
  rf.org_name         AS filer_org_name,
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
JOIN returns rf
  ON rf.filing_id = c.filing_id
JOIN grants_compat_v1 g
  ON g.filing_id = c.filing_id
ORDER BY rf.ein, c.tax_year DESC, total_amount DESC, g.recipient_name, g.recipient_ein
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
    # If only one provided, use that single year
    if min_year and not max_year:
        max_year = min_year
    elif max_year and not min_year:
        min_year = max_year
    # Swap if reversed
    if min_year and max_year and min_year > max_year:
        min_year, max_year = max_year, min_year
    return return_all, (state if state else None), min_year, max_year

def _build_where_for_candidates(return_all: bool, eins: List[str], state: Optional[str], min_year: Optional[int], max_year: Optional[int]):
    clauses = []
    params: List = []

    if not return_all:
        if not eins:
            # No EINs -> no results
            return "WHERE 1=0", []
        placeholders = ",".join("?" for _ in eins)
        clauses.append(f"c.ein IN ({placeholders})")
        params.extend(eins)

    if state:
        clauses.append("r.state = ?")
        params.append(state)

    if (min_year is not None) and (max_year is not None):
        clauses.append("c.tax_year BETWEEN ? AND ?")
        params.extend([min_year, max_year])

    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params

def _query(return_all: bool, eins: List[str], state: Optional[str], min_year: Optional[int], max_year: Optional[int]) -> List[Tuple]:
    conn = connect_ro()
    rows: List[Tuple] = []

    def _run_one(ein_subset: Optional[List[str]]):
        where_clause, params = _build_where_for_candidates(
            return_all if ein_subset is None else False,
            [] if ein_subset is None else ein_subset,
            state,
            min_year,
            max_year,
        )
        sql = _SQL_BASE.format(where_clause=("\n" + where_clause if where_clause else ""))
        cur = conn.execute(sql, params)
        return cur.fetchall()

    if not return_all:
        # chunk EIN IN() lists to avoid very large parameter lists
        CHUNK = 300
        for i in range(0, len(eins), CHUNK):
            rows.extend(_run_one(eins[i:i+CHUNK]))
    else:
        rows = _run_one(None)

    return rows

def run(form) -> Tuple[List[str], List[Tuple]]:
    return_all, state, min_year, max_year = _parse_filters(form)
    eins = [] if return_all else _parse_eins(form)
    return HEADERS, _query(return_all, eins, state, min_year, max_year)

def export_rows(form) -> Iterable[Tuple]:
    # For CSV export streams
    return_all, state, min_year, max_year = _parse_filters(form)
    eins = [] if return_all else _parse_eins(form)
    return _query(return_all, eins, state, min_year, max_year)
