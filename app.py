from flask import Flask, jsonify, request, render_template_string, Response, redirect, url_for
import importlib
import pkgutil
import io
import csv
import os
import tempfile
import traceback
import sys
import zipfile
from pathlib import Path
from common import DB_PATH, OLMS_DB_PATH, connect_olms_ro, connect_ro
from data_import import ImportManager, ImportOptions, PROJECT_ROOT
from datetime import datetime

# --- Flask ---
app = Flask(__name__)
DATA_IMPORT_MANAGER = ImportManager()

PLUGIN_PACKAGE = "queries"
PLUGIN_DIR = Path(__file__).parent / "queries"

# In-memory registry {key: module}
REGISTRY = {}
PLUGIN_FINGERPRINT = None


def plugin_fingerprint():
    """Return a cheap signature of query plugin files for auto-reload."""
    return tuple(
        sorted(
            (path.name, path.stat().st_mtime_ns)
            for path in PLUGIN_DIR.glob("*.py")
            if not path.name.startswith("_")
        )
    )


def load_plugins():
    """(Re)load all query plugins from queries/ directory."""
    loaded = {}
    if str(Path(__file__).parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).parent))
    for info in pkgutil.iter_modules([str(PLUGIN_DIR)]):
        if info.name.startswith("_"):
            continue
        module_name = f"{PLUGIN_PACKAGE}.{info.name}"
        try:
            if module_name in sys.modules:
                mod = importlib.reload(sys.modules[module_name])
            else:
                mod = importlib.import_module(module_name)
            required = ["META", "render_fields", "run", "export_rows"]
            if all(hasattr(mod, name) for name in required):
                loaded[mod.META["key"]] = mod
        except Exception as e:
            print(f"Failed to load plugin {module_name}: {e}", file=sys.stderr)
            traceback.print_exc()
    return loaded


def ensure_registry():
    global REGISTRY, PLUGIN_FINGERPRINT
    current_fingerprint = plugin_fingerprint()
    if not REGISTRY or current_fingerprint != PLUGIN_FINGERPRINT:
        REGISTRY = load_plugins()
        PLUGIN_FINGERPRINT = current_fingerprint


BASE_CSS = """
  :root {
    --border: #d8dde6;
    --ink: #202733;
    --muted: #647084;
    --panel: #f7f9fc;
    --primary: #1c78a6;
    --primary-dark: #125f85;
  }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, Segoe UI, Arial, sans-serif;
    color: var(--ink);
    max-width: 1200px;
    min-height: 100vh;
    margin: 0 auto;
    padding: 18px 24px 0;
    display: flex;
    flex-direction: column;
  }
  main { flex: 1; }
  a { color: var(--primary); }
  .site-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--border);
  }
  .title-wrap { display: flex; align-items: center; gap: 10px; min-width: 0; }
  h1 { margin: 0; font-size: 26px; line-height: 1.15; }
  h2 { margin-top: 24px; }
  .home-link {
    width: 34px;
    height: 34px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--primary-dark);
    background: #fff;
    flex: 0 0 auto;
  }
  .home-link:hover { background: var(--panel); }
  .home-link svg { width: 20px; height: 20px; }
  .header-actions { display: flex; gap: 10px; align-items: center; justify-content: flex-end; flex-wrap: wrap; }
  .toolbox-link {
    display: inline-flex;
    min-height: 34px;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 10px;
    color: var(--primary-dark);
    background: #fff;
    font-size: 14px;
    font-weight: 650;
    text-decoration: none;
    white-space: nowrap;
  }
  .toolbox-link:hover { background: var(--panel); }
  .brand-logo { width: 118px; height: auto; flex: 0 0 auto; }
  .footer {
    margin-top: 32px;
    padding: 18px 0;
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 13px;
    text-align: center;
  }
  .brand-link { display: inline-flex; flex: 0 0 auto; }
  .home-title-row {
    display: flex;
    align-items: baseline;
    gap: 14px;
    flex-wrap: wrap;
    margin-top: 24px;
  }
  .home-title-row h2 { margin: 0; }
  .home-title-row .note { margin: 0; }
  .module-sections { display: grid; gap: 26px; max-width: 900px; margin-top: 18px; }
  .module-section h3 { margin: 0 0 8px; font-size: 18px; }
  .module-list { display: grid; gap: 10px; }
  .module-row {
    display: grid;
    grid-template-columns: minmax(210px, 280px) 1fr;
    gap: 14px;
    align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid #eef1f5;
  }
  .module-button,
  button {
    border: 1px solid var(--primary-dark);
    background: var(--primary);
    color: #fff;
    border-radius: 6px;
    padding: 8px 12px;
    font: inherit;
    font-weight: 650;
    cursor: pointer;
    text-decoration: none;
    text-align: center;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 36px;
  }
  .module-button:hover,
  button:hover { background: var(--primary-dark); }
  button.secondary {
    color: var(--primary-dark);
    background: #fff;
    border-color: var(--border);
  }
  button.secondary:hover { background: var(--panel); }
  .help-icon {
    width: 20px;
    height: 20px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--muted);
    border-radius: 50%;
    color: var(--muted);
    background: #fff;
    font-size: 12px;
    font-weight: 700;
    cursor: help;
  }
  .description { color: var(--muted); line-height: 1.35; }
  .row { margin: 8px 0; }
  table { border-collapse: collapse; width: 100%; }
  th, td {
    text-align: left;
    padding: 6px;
    border-bottom: 1px solid #eee;
    vertical-align: top;
  }
  td {
    max-width: 300px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  body.ask-db td:nth-child(3) {
    max-width: 900px;
    white-space: pre-wrap;
    overflow: visible;
    text-overflow: clip;
    font-family: Consolas, "Courier New", monospace;
    font-size: 13px;
  }
  thead th { position: sticky; top: 0; background: #f6f6f6; border-bottom: 1px solid #ddd; }
  .toolbar { display:flex; gap:8px; align-items:center; flex-wrap: wrap; }
  .err { background:#ffecec; border:1px solid #f5b5b5; padding:8px; white-space:pre-wrap; }
  textarea { width:100%; }
  .running-msg {
    display: none;
    margin: 10px 0;
    padding: 10px;
    background: #fff8d6;
    border: 1px solid #e6d37a;
    border-radius: 6px;
    font-weight: 600;
  }
  body.is-running .running-msg { display: block; }
  body.is-running button { opacity: 0.6; cursor: not-allowed; }
  .sql-box {
    background: #f7f7f7;
    border: 1px solid #ddd;
    border-radius: 6px;
    padding: 10px;
    margin: 12px 0;
    white-space: pre-wrap;
    font-family: Consolas, "Courier New", monospace;
    font-size: 13px;
    overflow-x: auto;
  }
  .stats-summary {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 10px;
    margin: 18px 0;
  }
  .stat-tile {
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
    background: #fff;
  }
  .stat-label { color: var(--muted); font-size: 13px; }
  .stat-value { font-size: 22px; font-weight: 750; margin-top: 4px; }
  .stats-table-wrap { overflow:auto; max-height: 62vh; border: 1px solid var(--border); }
  .note { color: var(--muted); }
  @media (max-width: 700px) {
    body { padding: 14px 14px 0; }
    .site-header { align-items: flex-start; }
    .brand-logo { width: 88px; }
    h1 { font-size: 22px; }
    .home-title-row { gap: 6px 12px; }
    .module-row { grid-template-columns: 1fr; gap: 6px; }
    .module-button { justify-content: center; }
  }
"""

HOME_ICON = """
<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M3 11.5 12 4l9 7.5"></path>
  <path d="M5 10.5V20h5v-5h4v5h5v-9.5"></path>
</svg>
"""

LAYOUT_START = """
<!doctype html>
<title>{{ title or "IRS 990 & OLMS - Research Console" }}</title>
<meta charset="utf-8">
<style>{{ css | safe }}</style>

<body class="{% if qkey in ['ask_database', 'ask_database_v1', 'ask_database_v2'] %}ask-db{% endif %}">
<header class="site-header">
  <div class="title-wrap">
    <a class="home-link" href="{{ url_for('home') }}" aria-label="Home">{{ home_icon | safe }}</a>
    <h1>IRS 990 &amp; OLMS - Research Console</h1>
  </div>
  <div class="header-actions">
    {% if toolbox_home_url %}<a class="toolbox-link" href="{{ toolbox_home_url }}">All tools</a>{% endif %}
    <a class="brand-link" href="https://www.saveoregonschools.com" aria-label="Save Oregon Schools website">
      <img class="brand-logo" src="{{ url_for('static', filename='save-oregon-schools-logo.png') }}" alt="Save Oregon Schools">
    </a>
  </div>
</header>
<main>
"""

LAYOUT_END = """
</main>
<footer class="footer">
  Copyright &copy; {{ year }} Save Oregon Schools, LLC.
  <a href="https://www.saveoregonschools.com">www.saveoregonschools.com</a>
  |
  <a href="https://github.com/SaveOregonSchools/irs990-tool">Source code</a>
  |
  <a href="https://github.com/SaveOregonSchools/irs990-tool/blob/main/LICENSE">AGPLv3 license</a>
  |
  <a href="https://github.com/SaveOregonSchools/irs990-tool/blob/main/TRADEMARKS.md">Trademark notice</a>
</footer>
</body>
"""

HOME_HTML = LAYOUT_START + """
<div class="home-title-row">
  <h2>Home</h2>
  <p class="note">Select a module from the list below</p>
</div>

<div class="module-sections">
  {% for section in home_sections %}
    <section class="module-section">
      <h3>{{ section.title }}</h3>
      <div class="module-list">
        {% for item in section.entries %}
          <div class="module-row">
            <a class="module-button" href="{{ item.href }}">{{ item.label }}</a>
            <div class="description">{{ item.description }}</div>
          </div>
        {% endfor %}
      </div>
    </section>
  {% endfor %}
</div>
""" + LAYOUT_END

HOME_MENU = [
    (
        "Most Popular",
        [
            (
                "query",
                "ngo_core_data_lookup",
                "Core Data Lookup",
                "High-level info and financials by tax year for one or more nonprofits.",
            ),
            (
                "query",
                "ask_database",
                "Ask Database",
                "Ask a plain-English question involving nonprofit tax data.",
            ),
            (
                "query",
                "ngo_grants_in",
                "Grants Received",
                "See all grants received by a nonprofit by tax year.",
            ),
            (
                "query",
                "ngo_grants_out",
                "Grants Paid",
                "See all grants paid by a nonprofit by tax year.",
            ),
            (
                "query",
                "ngo_grants_io",
                "Grants Paid/Received",
                "See grants paid and received by a nonprofit by tax year.",
            ),
            (
                "query",
                "ngo_ein_by_name",
                "Find EINs by Organization Name",
                "Look up an EIN (Federal Tax ID) by organization name.",
            ),
            (
                "query",
                "people_lookup_v2",
                "Find Filings by Person Name",
                "Find where person names appear in tax filings.",
            ),
        ],
    ),
    (
        "Labor / OLMS",
        [
            (
                "query",
                "olms_union_deep_dive",
                "Union Deep Dive",
                "Single-union identity, trends, filing history, grants, payees, and compensation.",
            ),
            (
                "query",
                "olms_filing_compliance",
                "Filing Compliance / Timeliness",
                "Separate observed late filings from conservative potential-missing filing flags.",
            ),
            (
                "query",
                "olms_grants_paid",
                "Grants / Contributions Paid",
                "Explore code 503 annual payee totals or itemized transactions.",
            ),
            (
                "query",
                "olms_vendors_paid",
                "Vendors / Contractors / Payees",
                "Explore union-reported vendors, consultants, service providers, and other payees.",
            ),
            (
                "query",
                "olms_counterparty_explorer",
                "Grantee / Vendor Explorer",
                "Find a counterparty and see all OLMS unions that reported paying it.",
            ),
            (
                "query",
                "olms_irs_match_audit",
                "OLMS / IRS Match Audit",
                "Review deterministic F_NUM-to-EIN matches and candidates.",
            ),
            (
                "query",
                "olms_import_audit",
                "OLMS Import / Data Quality",
                "Review source hashes, row counts, repairs, quarantines, duplicates, and orphans.",
            ),
        ],
    ),
    (
        "Data Maintenance",
        [
            (
                "data_import",
                "data_import",
                "Import New IRS Data",
                "Guided XML append, optional EO-BMF installation, and deterministic enhanced grant matching.",
            ),
        ],
    ),
    (
        "Other Modules",
        [
            (
                "stats",
                "stats",
                "Database Statistics",
                "Review statistics of what is in this IRS database.",
            ),
            (
                "query",
                "nonprofit_deep_dive",
                "Nonprofit Deep Dive",
                "Single-EIN profile with trend charts, yearly summaries, top grantors, and compensation.",
            ),
            (
                "query",
                "fraud_risk_dashboard",
                "Fraud & Risk Indicators",
                "Single-EIN dashboard of financial, governance, lobbying, grant, contractor, and related-org indicators.",
            ),
            (
                "query",
                "filings_by_eins",
                "Filings by EIN(s)",
                "Basic list of available tax filings by EIN.",
            ),
            (
                "query",
                "ngo_contractors_out",
                "Contractors",
                "Show top contractors paid by a nonprofit by tax year.",
            ),
            (
                "query",
                "lobbying_political_activity",
                "Lobbying & Political Activity",
                "Explore Schedule C lobbying, political campaign, 527, dues/proxy-tax, and 990-PF indicators.",
            ),
            (
                "query",
                "ngo_related_orgs_sched_r",
                "Schedule R: Related Orgs",
                "Show related organizations, if applicable, by nonprofit and tax year.",
            ),
        ],
    ),
]

QUERY_HTML = LAYOUT_START + """
<form method="post" action="{{ url_for('select') }}">
  <div class="toolbar">
    <label for="qkey"><b>Query:</b></label>

    <select name="qkey" id="qkey"
            onchange="this.form.submit()">
      {% for key, mod in query_options %}
        <option value="{{ key }}" {% if key == qkey %}selected{% endif %}>{{ mod.META["name"] }}</option>
      {% endfor %}
    </select>
  </div>
</form>

{% if qkey %}
  <hr>
  <h2>{{ registry[qkey].META["name"] }}</h2>
  <p>{{ registry[qkey].META.get("description","") }}</p>

  <form method="post" action="{{ url_for('run') }}" onsubmit="return showRunningMessage(event, this);">
    <input type="hidden" name="qkey" value="{{ qkey }}">
    {{ registry[qkey].render_fields(form or {}) | safe }}
    <div class="toolbar">
      {% if not hide_preview_limit %}
        <label>Preview row limit:</label>
        <input type="number" name="_limit" value="{{ (form or {}).get('_limit','500') }}" min="1" style="width:100px">
      {% endif %}
      <button type="submit">{{ run_button_label }}</button>
      {% if pdf_export %}
        {% if export_controls_visible %}
          <button formaction="{{ url_for('export_pdf') }}" formmethod="post" formtarget="_blank">Export PDF</button>
        {% endif %}
      {% elif not hide_csv_export %}
        <button formaction="{{ url_for('export') }}" formmethod="post">Export CSV (full result)</button>
      {% endif %}
      {% if download_filings and export_controls_visible %}
        <button formaction="{{ url_for('download_filings') }}" formmethod="post">Download Filings</button>
        <span class="help-icon" tabindex="0" role="img"
              aria-label="Downloads a ZIP containing the XML versions of all displayed tax filings."
              title="This will zip the XML versions of all displayed tax filings and begin a download.">?</span>
      {% endif %}
    </div>

    <div class="running-msg">
      Running query. Please wait...
    </div>
  </form>

  {% if error %}
    <div class="err"><b>Error:</b>\n{{ error }}</div>
  {% endif %}

  {% if headers and rows is not none %}
    {% if custom_results_html %}
      {{ custom_results_html | safe }}

    {% elif headers and headers[0] == 'generated_sql' and rows|length > 0 %}
      <p>Showing up to <b>{{ (form or {}).get('_limit','500') }}</b> rows. Preview contains <b>{{ len(rows) }}</b> rows.</p>
      <h3>Generated SQL</h3>
      <div class="sql-box">{{ rows[0][0] }}</div>

      <div style="overflow:auto; max-height:60vh; border:1px solid #ddd;">
        <table>
          <thead>
            <tr>
              {% for h in headers[1:] %}
                <th>{{ h }}</th>
              {% endfor %}
            </tr>
          </thead>
          <tbody>
            {% for r in rows %}
              <tr>
                {% for v in r[1:] %}
                  <td title="{{ v|e }}">{{ v }}</td>
                {% endfor %}
              </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>

    {% else %}
      <p>Showing up to <b>{{ (form or {}).get('_limit','500') }}</b> rows. Preview contains <b>{{ len(rows) }}</b> rows.</p>
      <div style="overflow:auto; max-height:60vh; border:1px solid #ddd;">
        <table>
          <thead><tr>{% for h in headers %}<th>{{ h }}</th>{% endfor %}</tr></thead>
          <tbody>
            {% for r in rows %}
              <tr>{% for v in r %}<td title="{{ v|e }}">{{ v }}</td>{% endfor %}</tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    {% endif %}
  {% endif %}

{% endif %}
<script>
  function showRunningMessage(event, form) {
    document.body.classList.add("is-running");

    const submitter = event.submitter;
    const submitAction = submitter ? submitter.getAttribute("formaction") : "";
    const isExport = submitAction === "{{ url_for('export') }}" ||
      submitAction === "{{ url_for('export_pdf') }}" ||
      submitAction === "{{ url_for('download_filings') }}";

    const buttons = form.querySelectorAll("button");
    buttons.forEach(function(btn) {
      btn.disabled = true;
    });

    // Export-style actions usually complete outside this page,
    // so re-enable the UI after a short delay.
    if (isExport) {
      setTimeout(function() {
        document.body.classList.remove("is-running");
        buttons.forEach(function(btn) {
          btn.disabled = false;
        });
      }, 1500);
    }

    return true;
  }
</script>
""" + LAYOUT_END

DATA_IMPORT_HTML = LAYOUT_START + """
<div class="home-title-row">
  <h2>Import New IRS Data</h2>
  <p class="note">Append XML filings and run deterministic post-import processing</p>
</div>

{% if error %}<pre class="err">{{ error }}</pre>{% endif %}

{% if state.status == 'running' %}
  <p><b>An import is running.</b> This page refreshes automatically. Keep the IRS 990 app process open.</p>
{% elif state.status == 'completed' %}
  <p><b>Import and deterministic matching completed.</b> AI adjudication was not run.</p>
{% elif state.status == 'failed' %}
  <p><b>The import stopped at a failed step.</b> No later steps were run.</p>
{% endif %}

{% if state.status in ('running', 'completed', 'failed') %}
  <h3>Run {{ state.run_id }}</h3>
  <ol>
    {% for step in state.steps %}
      <li><b>{{ step.status|upper }}</b> — {{ step.label }}</li>
    {% endfor %}
  </ol>
  {% if state.error %}<pre class="err">{{ state.error }}</pre>{% endif %}
  {% if state.log_path %}<p class="note">Full log: <code>{{ state.log_path }}</code></p>{% endif %}
  <h3>Recent output</h3>
  <pre class="sql-box" style="max-height:460px; overflow:auto; white-space:pre-wrap;">{{ state.logs|join('\n') }}</pre>
{% endif %}

{% if state.status == 'completed' and state.instructions %}
  <h3>Next: external AI adjudication</h3>
  <p>Generate packet batches, process them with <code>grant_ai_batch_worker.py</code>, then dry-run and perform the decision import. Finally, rebuild the applied layer:</p>
  {% for command in state.instructions %}
    <pre class="sql-box" style="white-space:pre-wrap;">{{ command }}</pre>
  {% endfor %}
  <p>See <code>docs/grant-matching.md</code> for the Linux worker command and audit fields to review.</p>
{% endif %}

{% if state.status != 'running' %}
  <form method="post" action="{{ url_for('data_import_page') }}">
    <div class="row">
      <label><b>Directories containing new IRS XML files:</b></label>
      <div id="xml-directory-list">
        {% for xml_dir in form.xml_dirs %}
          <div class="xml-directory-row" style="display:flex; gap:8px; margin-top:8px; align-items:center;">
            <input name="xml_dir" value="{{ xml_dir }}" required style="width:min(100%,700px); flex:1;">
            <button type="button" class="secondary browse-directory">Browse</button>
            <button type="button" class="secondary remove-directory" title="Remove this directory">−</button>
          </div>
        {% endfor %}
      </div>
      <button type="button" id="add-xml-directory" class="secondary" style="margin-top:8px;" title="Add another XML directory">+ Add directory</button>
      <div class="note">Each directory is searched recursively, preflighted, and appended in order. Duplicate paths and existing filings are skipped.</div>
    </div>

    <div class="row">
      <label><input type="checkbox" name="bmf_updated" {% if form.bmf_updated %}checked{% endif %}> <b>EO-BMF files were also updated</b></label>
    </div>

    <div class="row">
      <label for="bmf_source_dir"><b>Optional directory containing downloaded eo1.csv through eo4.csv:</b></label><br>
      <input id="bmf_source_dir" name="bmf_source_dir" value="{{ form.bmf_source_dir }}" style="width:min(100%,760px);">
      <div class="note">If supplied, the current project copies are backed up before the new files are installed. Leave blank if you already replaced the project files.</div>
    </div>

    <details>
      <summary>Database paths</summary>
      <div class="row">
        <label for="db_path"><b>Main IRS database:</b></label><br>
        <input id="db_path" name="db_path" value="{{ form.db_path }}" required style="width:min(100%,760px);">
      </div>
      <div class="row">
        <label for="work_db_path"><b>Enhanced grant work database:</b></label><br>
        <input id="work_db_path" name="work_db_path" value="{{ form.work_db_path }}" style="width:min(100%,760px);">
      </div>
    </details>

    <div class="row" style="margin-top:18px;">
      <label><input type="checkbox" name="confirm" required> I understand this will write to the databases and can take many hours.</label>
    </div>
    <div class="toolbar"><button type="submit">Start Data Import</button></div>
  </form>

  <dialog id="directory-browser" style="width:min(760px,94vw); border:1px solid var(--border); border-radius:8px; padding:18px;">
    <h3 style="margin-top:0;">Choose an XML directory</h3>
    <div id="browser-current" class="sql-box" style="word-break:break-all;"></div>
    <div class="toolbar">
      <button type="button" id="browser-parent" class="secondary">Up one level</button>
      <button type="button" id="browser-select">Select this directory</button>
      <button type="button" id="browser-cancel" class="secondary">Cancel</button>
    </div>
    <div id="browser-directories" style="display:grid; gap:6px; max-height:430px; overflow:auto;"></div>
    <div id="browser-error" class="err" style="display:none; margin-top:8px;"></div>
  </dialog>

  <script>
  (function () {
    const list = document.getElementById('xml-directory-list');
    const dialog = document.getElementById('directory-browser');
    const currentLabel = document.getElementById('browser-current');
    const children = document.getElementById('browser-directories');
    const errorBox = document.getElementById('browser-error');
    const parentButton = document.getElementById('browser-parent');
    let activeInput = null;
    let currentPath = {{ directory_browser_start|tojson }};
    let parentPath = null;

    function addDirectory(value) {
      const row = document.createElement('div');
      row.className = 'xml-directory-row';
      row.style.cssText = 'display:flex; gap:8px; margin-top:8px; align-items:center;';
      const input = document.createElement('input');
      input.name = 'xml_dir'; input.required = true; input.value = value || '';
      input.style.cssText = 'width:min(100%,700px); flex:1;';
      const browse = document.createElement('button');
      browse.type = 'button'; browse.className = 'secondary browse-directory'; browse.textContent = 'Browse';
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'secondary remove-directory'; remove.title = 'Remove this directory'; remove.textContent = '−';
      row.append(input, browse, remove); list.appendChild(row);
    }

    document.getElementById('add-xml-directory').addEventListener('click', function () { addDirectory(''); });
    list.addEventListener('click', function (event) {
      const row = event.target.closest('.xml-directory-row');
      if (!row) return;
      if (event.target.classList.contains('remove-directory')) {
        if (list.children.length > 1) row.remove(); else row.querySelector('input').value = '';
      }
      if (event.target.classList.contains('browse-directory')) {
        activeInput = row.querySelector('input');
        loadDirectory(activeInput.value || {{ directory_browser_start|tojson }});
        dialog.showModal();
      }
    });

    async function loadDirectory(path) {
      errorBox.style.display = 'none';
      try {
        const response = await fetch({{ url_for('data_import_directories')|tojson }} + '?path=' + encodeURIComponent(path));
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || 'Could not list that directory.');
        currentPath = payload.path; parentPath = payload.parent;
        currentLabel.textContent = currentPath;
        parentButton.disabled = !parentPath;
        children.replaceChildren();
        payload.directories.forEach(function (entry) {
          const button = document.createElement('button');
          button.type = 'button'; button.className = 'secondary'; button.style.justifyContent = 'flex-start';
          button.textContent = entry.name; button.addEventListener('click', function () { loadDirectory(entry.path); });
          children.appendChild(button);
        });
      } catch (error) {
        errorBox.textContent = error.message; errorBox.style.display = 'block';
      }
    }

    parentButton.addEventListener('click', function () { if (parentPath) loadDirectory(parentPath); });
    document.getElementById('browser-select').addEventListener('click', function () {
      if (activeInput) activeInput.value = currentPath;
      dialog.close();
    });
    document.getElementById('browser-cancel').addEventListener('click', function () { dialog.close(); });
  }());
  </script>
{% endif %}

{% if state.status == 'running' %}
<script>window.setTimeout(function () { window.location.reload(); }, 3000);</script>
{% endif %}
""" + LAYOUT_END

STATS_HTML = LAYOUT_START + """
<h2>Database Statistics</h2>

{% if error %}
  <div class="err"><b>Error:</b>\n{{ error }}</div>
{% endif %}

{% if updated_at %}
  <p class="note">Cached statistics last refreshed: <b>{{ updated_at }}</b></p>
{% else %}
  <p class="note">Cached statistics have not been generated yet. Run <code>python refresh_data_stats.py</code> or the enhanced grant batch workflow.</p>
{% endif %}

<div class="stats-summary">
  {% for item in summary %}
    <div class="stat-tile">
      <div class="stat-label">{{ item.label }}</div>
      <div class="stat-value">{{ item.value }}</div>
    </div>
  {% endfor %}
</div>

{% if rows %}
  <div class="stats-table-wrap">
    <table>
      <thead>
        <tr>
          <th>Section</th>
          <th>Metric</th>
          <th>Bucket</th>
          <th>Count</th>
          <th>Signatures</th>
          <th>Grants</th>
          <th>Total Amount</th>
          <th>% Grants</th>
          <th>% Section</th>
          <th>Notes</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
          <tr>
            <td>{{ r.section }}</td>
            <td>{{ r.metric }}</td>
            <td>{{ r.bucket }}</td>
            <td>{{ r.count_fmt }}</td>
            <td>{{ r.signatures_fmt }}</td>
            <td>{{ r.grants_represented_fmt }}</td>
            <td>{{ r.total_amount_fmt }}</td>
            <td>{{ r.pct_of_grants_fmt }}</td>
            <td>{{ r.pct_of_section_fmt }}</td>
            <td title="{{ r.notes|e }}">{{ r.notes }}</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
{% endif %}
""" + LAYOUT_END


def _template_context(**extra):
    ctx = {
        "css": BASE_CSS,
        "home_icon": HOME_ICON,
        "toolbox_home_url": os.environ.get("TOOLBOX_HOME_URL", "").strip() or None,
        "year": datetime.now().year,
    }
    ctx.update(extra)
    return ctx


def _build_home_sections():
    seen_query_keys = set()
    sections = []
    for title, entries in HOME_MENU:
        items = []
        for entry in entries:
            item_type, key, label = entry[:3]
            if item_type == "stats":
                description = entry[3] if len(entry) > 3 else ""
                items.append(
                    {
                        "label": label,
                        "href": url_for("stats_page"),
                        "description": description,
                    }
                )
                continue
            if item_type == "data_import":
                items.append(
                    {
                        "label": label,
                        "href": url_for("data_import_page"),
                        "description": entry[3] if len(entry) > 3 else "",
                    }
                )
                continue

            mod = REGISTRY.get(key)
            if not mod:
                continue
            seen_query_keys.add(key)
            description = entry[3] if len(entry) > 3 else mod.META.get("description", "")
            items.append(
                {
                    "label": label,
                    "href": url_for("query_page", qkey=key),
                    "description": description,
                }
            )
        if items:
            sections.append({"title": title, "entries": items})

    extra_items = []
    for key, mod in REGISTRY.items():
        if key in seen_query_keys:
            continue
        extra_items.append(
            {
                "label": mod.META["name"],
                "href": url_for("query_page", qkey=key),
                "description": mod.META.get("description", ""),
            }
        )
    if extra_items:
        if sections and sections[-1]["title"] == "Other Modules":
            sections[-1]["entries"].extend(extra_items)
        else:
            sections.append({"title": "Other Modules", "entries": extra_items})
    return sections


def _query_options():
    return sorted(
        REGISTRY.items(),
        key=lambda item: item[1].META.get("name", item[0]).casefold(),
    )


def _render_home():
    ensure_registry()
    return render_template_string(
        HOME_HTML,
        **_template_context(title="IRS 990 - Home", qkey=None, home_sections=_build_home_sections()),
    )


def _module_flag(qkey, name):
    return bool(qkey in REGISTRY and getattr(REGISTRY[qkey], name, False))


def _render_query(qkey, form=None, headers=None, rows=None, error=None):
    ensure_registry()
    custom_results_html = None
    if headers and rows is not None and qkey in REGISTRY and hasattr(REGISTRY[qkey], "render_results"):
        custom_results_html = REGISTRY[qkey].render_results(form or {}, headers, rows)
    exports_require_results = _module_flag(qkey, "EXPORTS_REQUIRE_RESULTS")
    export_controls_visible = not exports_require_results or (
        error is None and rows is not None and bool(rows)
    )
    return render_template_string(
        QUERY_HTML,
        **_template_context(
            title="IRS 990 - Query Console",
            registry=REGISTRY,
            query_options=_query_options(),
            qkey=qkey,
            form=form,
            headers=headers,
            rows=rows,
            error=error,
            len=len,
            custom_results_html=custom_results_html,
            hide_preview_limit=_module_flag(qkey, "HIDE_PREVIEW_LIMIT"),
            hide_csv_export=_module_flag(qkey, "HIDE_CSV_EXPORT"),
            pdf_export=_module_flag(qkey, "PDF_EXPORT"),
            download_filings=_module_flag(qkey, "DOWNLOAD_FILINGS"),
            export_controls_visible=export_controls_visible,
            run_button_label=getattr(REGISTRY[qkey], "RUN_BUTTON_LABEL", "Run Query") if qkey in REGISTRY else "Run Query",
        ),
    )


def _fmt_int(value):
    if value in (None, ""):
        return ""
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def _fmt_money(value):
    if value in (None, ""):
        return ""
    try:
        return f"${float(value):,.0f}"
    except Exception:
        return str(value)


def _fmt_pct(value):
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.2f}%"
    except Exception:
        return str(value)


def _fmt_bytes(value):
    try:
        size = float(value or 0)
    except Exception:
        return ""
    units = ["bytes", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:,.1f} {units[idx]}" if idx else f"{int(size):,} {units[idx]}"


def _row_value(rows, section, metric, bucket=""):
    for row in rows:
        if row["section"] == section and row["metric"] == metric and row["bucket"] == bucket:
            return row
    return None


def _read_stats_cache():
    rows = []
    summary = []
    updated_at = None
    error = None
    conn = None

    try:
        db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        summary.append({"label": "Database File", "value": _fmt_bytes(db_size)})
    except Exception:
        pass

    try:
        conn = connect_ro()
        stat_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_data_stats' LIMIT 1"
        ).fetchone()
        if not stat_table:
            return summary, rows, updated_at, None

        meta_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_data_stats_meta' LIMIT 1"
        ).fetchone()
        if meta_table:
            meta = conn.execute(
                "SELECT value FROM app_data_stats_meta WHERE key='refreshed_at'"
            ).fetchone()
            if meta:
                updated_at = meta[0]

        cur = conn.execute(
            """
            SELECT section, metric, bucket, count, signatures, grants_represented,
                   total_amount, pct_of_grants, pct_of_section, notes
            FROM app_data_stats
            ORDER BY
              CASE section
                WHEN 'database' THEN 0
                WHEN 'filings' THEN 1
                WHEN 'grant_match_summary' THEN 2
                ELSE 3
              END,
              section, metric, bucket
            """
        )
        columns = [d[0] for d in cur.description]
        for db_row in cur.fetchall():
            item = dict(zip(columns, db_row))
            item["count_fmt"] = _fmt_int(item.get("count"))
            item["signatures_fmt"] = _fmt_int(item.get("signatures"))
            item["grants_represented_fmt"] = _fmt_int(item.get("grants_represented"))
            item["total_amount_fmt"] = _fmt_money(item.get("total_amount"))
            item["pct_of_grants_fmt"] = _fmt_pct(item.get("pct_of_grants"))
            item["pct_of_section_fmt"] = _fmt_pct(item.get("pct_of_section"))
            rows.append(item)
    except Exception:
        error = traceback.format_exc()
    finally:
        if conn is not None:
            conn.close()

    total_filings = _row_value(rows, "filings", "total_filings")
    if total_filings:
        summary.append({"label": "Tax Filings", "value": _fmt_int(total_filings.get("count"))})
    total_grants = _row_value(rows, "raw_grants", "total_grants")
    if total_grants:
        summary.append({"label": "Grant Rows", "value": _fmt_int(total_grants.get("count"))})
    enhanced = _row_value(rows, "grant_match_summary", "enhanced_grant_outcomes", "enhanced_match")
    if enhanced:
        summary.append({"label": "Enhanced Matches", "value": _fmt_int(enhanced.get("count"))})

    olms_conn = None
    if OLMS_DB_PATH.exists():
        try:
            summary.append({"label": "OLMS Database File", "value": _fmt_bytes(OLMS_DB_PATH.stat().st_size)})
            olms_conn = connect_olms_ro()
            cache_exists = olms_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='olms_stats_cache'"
            ).fetchone()
            if cache_exists:
                olms_stat_rows = olms_conn.execute(
                    "SELECT metric,bucket,value,notes,updated_at FROM olms_stats_cache ORDER BY metric,bucket"
                ).fetchall()
                for metric, bucket, value, notes, stat_updated in olms_stat_rows:
                    rows.append(
                        {
                            "section": "olms",
                            "metric": metric,
                            "bucket": bucket,
                            "count": value,
                            "signatures": None,
                            "grants_represented": None,
                            "total_amount": None,
                            "pct_of_grants": None,
                            "pct_of_section": None,
                            "notes": notes,
                            "count_fmt": _fmt_int(value),
                            "signatures_fmt": "",
                            "grants_represented_fmt": "",
                            "total_amount_fmt": "",
                            "pct_of_grants_fmt": "",
                            "pct_of_section_fmt": "",
                        }
                    )
                    if metric == "unique_labor_organizations" and not bucket:
                        summary.append({"label": "OLMS Organizations", "value": _fmt_int(value)})
                    elif metric == "total_reports" and not bucket:
                        summary.append({"label": "OLMS Reports", "value": _fmt_int(value)})
                    elif metric == "potential_missing_filings" and not bucket:
                        summary.append({"label": "Potential Missing LM Filings", "value": _fmt_int(value)})
                if not updated_at and olms_stat_rows:
                    updated_at = olms_stat_rows[-1][4]
        except Exception:
            olms_error = traceback.format_exc()
            error = (error + "\n" if error else "") + olms_error
        finally:
            if olms_conn is not None:
                olms_conn.close()

    return summary, rows, updated_at, error


@app.route("/", methods=["GET"])
def home():
    return _render_home()


@app.route("/query/<qkey>", methods=["GET"])
def query_page(qkey):
    ensure_registry()
    if qkey not in REGISTRY:
        return redirect(url_for("home"))
    return _render_query(qkey, form=request.args.to_dict(flat=True), headers=None, rows=None, error=None)


@app.route("/stats", methods=["GET"])
def stats_page():
    summary, rows, updated_at, error = _read_stats_cache()
    return render_template_string(
        STATS_HTML,
        **_template_context(
            title="IRS 990 - Database Statistics",
            qkey=None,
            summary=summary,
            rows=rows,
            updated_at=updated_at,
            error=error,
        ),
    )


def _data_import_form(values=None):
    values = values or {}
    default_work_db = Path(os.getenv("IRS_GRANT_WORK_DB_PATH", DB_PATH.parent / "grant_matching_work.db"))
    return {
        "xml_dirs": values.get("xml_dirs") or [""],
        "bmf_updated": values.get("bmf_updated") in (True, "on", "1", "true"),
        "bmf_source_dir": values.get("bmf_source_dir", ""),
        "db_path": values.get("db_path", str(DB_PATH)),
        "work_db_path": values.get("work_db_path", str(default_work_db)),
    }


@app.route("/data-import", methods=["GET", "POST"])
def data_import_page():
    error = None
    posted = request.form.to_dict(flat=True) if request.method == "POST" else None
    if posted is not None:
        posted["xml_dirs"] = request.form.getlist("xml_dir")
    form = _data_import_form(posted)
    if request.method == "POST":
        try:
            if request.form.get("confirm") != "on":
                raise ValueError("Confirm the database update before starting.")
            options = ImportOptions.from_values(
                xml_dirs=form["xml_dirs"],
                db_path=form["db_path"],
                work_db_path=form["work_db_path"],
                bmf_updated=form["bmf_updated"],
                bmf_source_dir=form["bmf_source_dir"],
                project_dir=PROJECT_ROOT,
            )
            DATA_IMPORT_MANAGER.start(options)
            return redirect(url_for("data_import_page"))
        except Exception as exc:
            error = str(exc)
    return render_template_string(
        DATA_IMPORT_HTML,
        **_template_context(
            title="IRS 990 - Import New Data",
            qkey=None,
            form=form,
            state=DATA_IMPORT_MANAGER.snapshot(),
            error=error,
            directory_browser_start=str(_directory_browser_root()),
        ),
    )


def _directory_browser_root():
    configured = (os.getenv("IRS_XML_ROOT") or "").strip()
    root = Path(configured).expanduser().resolve() if configured else PROJECT_ROOT
    return root if root.is_dir() else PROJECT_ROOT


@app.route("/data-import/directories", methods=["GET"])
def data_import_directories():
    root = _directory_browser_root()
    requested = (request.args.get("path") or "").strip()
    candidate = Path(requested).expanduser().resolve() if requested else root
    try:
        candidate.relative_to(root)
    except ValueError:
        return jsonify({"error": f"Browse selections must stay beneath {root}"}), 400
    if not candidate.is_dir():
        return jsonify({"error": f"Directory does not exist: {candidate}"}), 404

    directories = []
    try:
        for child in candidate.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                directories.append({"name": child.name, "path": str(child.resolve())})
    except OSError as exc:
        return jsonify({"error": f"Could not read {candidate}: {exc}"}), 403
    directories.sort(key=lambda item: item["name"].casefold())
    parent = str(candidate.parent) if candidate != root else None
    return jsonify({"path": str(candidate), "parent": parent, "directories": directories})


@app.route("/refresh", methods=["POST"])
def refresh():
    global REGISTRY
    REGISTRY = load_plugins()
    return redirect(url_for("home"))


@app.route("/select", methods=["POST"])
def select():
    ensure_registry()
    qkey = request.form.get("qkey")
    if qkey not in REGISTRY:
        return redirect(url_for("home"))
    return redirect(url_for("query_page", qkey=qkey))


@app.route("/run", methods=["GET", "POST"])
def run():
    if request.method == "GET":
        return redirect(url_for("home"))
    ensure_registry()
    qkey = request.form.get("qkey")
    if qkey not in REGISTRY:
        return redirect(url_for("home"))
    form = request.form.to_dict(flat=True)
    error = None
    headers, rows = None, None
    try:
        headers, rows = REGISTRY[qkey].run(form)
        if not _module_flag(qkey, "DISABLE_ROW_LIMIT"):
            try:
                lim = max(1, int(form.get("_limit", "500")))
            except Exception:
                lim = 500
            rows = rows[:lim]
    except Exception:
        error = traceback.format_exc()
    return _render_query(qkey, form=form, headers=headers, rows=rows, error=error)


@app.route("/export", methods=["GET", "POST"])
def export():
    if request.method == "GET":
        return redirect(url_for("home"))
    ensure_registry()
    qkey = request.form.get("qkey")
    form = request.form.to_dict(flat=True)
    if qkey not in REGISTRY:
        return "Unknown query key.", 400

    def generate():
        if hasattr(REGISTRY[qkey], "export_headers"):
            headers = REGISTRY[qkey].export_headers(form)
        else:
            headers = getattr(REGISTRY[qkey], "HEADERS", REGISTRY[qkey].META.get("headers"))
        yield ",".join(headers) + "\r\n"
        for row in REGISTRY[qkey].export_rows(form):
            buf = io.StringIO(newline="")
            writer = csv.writer(buf, lineterminator="\r\n")
            writer.writerow(row)
            yield buf.getvalue()

    ts = datetime.now().strftime("%m-%d-%Y_%H%M")
    base = REGISTRY[qkey].META.get("key", qkey)
    filename = f"{base}_{ts}.csv"

    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/export_pdf", methods=["GET", "POST"])
def export_pdf():
    if request.method == "GET":
        return redirect(url_for("home"))
    ensure_registry()
    qkey = request.form.get("qkey")
    form = request.form.to_dict(flat=True)
    if qkey not in REGISTRY:
        return "Unknown query key.", 400
    if not hasattr(REGISTRY[qkey], "render_pdf_export"):
        return "This module does not support PDF export.", 400

    return Response(
        REGISTRY[qkey].render_pdf_export(form),
        mimetype="text/html; charset=utf-8",
    )


def _flat_archive_name(filename, used_names):
    candidate = Path(filename).name
    stem = Path(candidate).stem
    suffix = Path(candidate).suffix
    counter = 2
    while candidate.casefold() in used_names:
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    used_names.add(candidate.casefold())
    return candidate


def _create_filings_zip(source_paths):
    temp_dir = app.config.get("DOWNLOAD_TEMP_DIR")
    fd, temp_name = tempfile.mkstemp(prefix="irs990_filings_", suffix=".zip", dir=temp_dir)
    os.close(fd)
    zip_path = Path(temp_name)
    try:
        used_names = set()
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source_path in source_paths:
                source_path = Path(source_path)
                archive.write(source_path, _flat_archive_name(source_path.name, used_names))
        return zip_path
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise


def _stream_file_then_delete(path, chunk_size=1024 * 1024):
    try:
        with Path(path).open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    finally:
        Path(path).unlink(missing_ok=True)


@app.route("/download_filings", methods=["GET", "POST"])
def download_filings():
    if request.method == "GET":
        return redirect(url_for("home"))
    ensure_registry()
    qkey = request.form.get("qkey")
    form = request.form.to_dict(flat=True)
    if qkey not in REGISTRY:
        return "Unknown query key.", 400
    module = REGISTRY[qkey]
    if not getattr(module, "DOWNLOAD_FILINGS", False) or not hasattr(module, "filing_xml_paths"):
        return "This module does not support filing downloads.", 400

    try:
        source_paths = list(module.filing_xml_paths(form))
        if not source_paths:
            return "No displayed tax filings were available to download.", 404
        zip_path = _create_filings_zip(source_paths)
    except (FileNotFoundError, ValueError) as exc:
        return str(exc), 404

    ein = "".join(ch for ch in (form.get("ein") or "") if ch.isdigit())
    ts = datetime.now().strftime("%m-%d-%Y")
    filename = f"nonprofit_deep_dive_{ein}_filings_{ts}.zip"
    return Response(
        _stream_file_then_delete(zip_path),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    app.run(debug=True)
