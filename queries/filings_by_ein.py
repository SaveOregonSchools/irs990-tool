# queries/filings_by_eins.py
from typing import List, Iterable, Tuple
from common import connect_ro, normalize_eins

META = {
    "key": "filings_by_eins",
    "name": "Filings by EIN(s)",
    "description": (
        "Enter one or more EINs (comma/semicolon/space/newline separated). "
        "Returns EIN, tax year, filing type, filing_id, and org name."
    ),
}

HEADERS = ["ein", "org_name", "tax_year", "filing_type", "filing_id"]

def render_fields(form) -> str:
    val = (form or {}).get("ein_list", "")
    return f"""
    <div class="row">
      <label for="ein_list"><b>EIN(s):</b></label><br>
      <textarea id="ein_list" name="ein_list" rows="6" placeholder="e.g. 131624102, 941156365; 52-6043385
123456789"></textarea>
      <script>document.getElementById('ein_list').value = {val!r};</script>
      <div style="color:#666; font-size: 90%; margin-top:4px;">
        Separate by commas, semicolons, spaces, or new lines. Non-digits ignored; we’ll keep valid 9-digit EINs.
      </div>
    </div>
    """

_SQL_TEMPLATE = """
    SELECT c.ein,
           r.org_name AS org_name,
           c.tax_year,
           c.return_type AS filing_type,
           c.filing_id
    FROM canonical_by_ein_year c
    LEFT JOIN returns r ON r.filing_id = c.filing_id
    WHERE c.ein IN ({placeholders})
    ORDER BY c.ein, c.tax_year DESC, c.filing_id
"""

def _query(eins: List[str]) -> List[Tuple]:
    if not eins:
        return []
    rows: List[Tuple] = []
    conn = connect_ro()
    CHUNK = 300  # stay under SQLite’s parameter limit
    for i in range(0, len(eins), CHUNK):
        chunk = eins[i:i+CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        sql = _SQL_TEMPLATE.format(placeholders=placeholders)
        cur = conn.execute(sql, chunk)
        rows.extend(cur.fetchall())
    return rows

def _parse_eins(form) -> List[str]:
    text = (form or {}).get("ein_list", "")
    return normalize_eins(text)

def run(form):
    eins = _parse_eins(form)
    rows = _query(eins)
    return HEADERS, rows

def export_rows(form) -> Iterable[Tuple]:
    eins = _parse_eins(form)
    return _query(eins)
