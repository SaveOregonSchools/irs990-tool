from flask import Flask, request, render_template_string, Response, redirect, url_for
import importlib
import pkgutil
import io
import csv
import traceback
import sys
from pathlib import Path
from common import DB_PATH, connect_ro
from datetime import datetime

# --- Flask ---
app = Flask(__name__)

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
    .module-button { justify-content: flex-start; }
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
<title>{{ title or "IRS 990 - Query Console" }}</title>
<meta charset="utf-8">
<style>{{ css | safe }}</style>

<body class="{% if qkey in ['ask_database', 'ask_database_v1', 'ask_database_v2'] %}ask-db{% endif %}">
<header class="site-header">
  <div class="title-wrap">
    <a class="home-link" href="{{ url_for('home') }}" aria-label="Home">{{ home_icon | safe }}</a>
    <h1>IRS 990 - Query Console</h1>
  </div>
  <a class="brand-link" href="https://www.saveoregonschools.com" aria-label="Save Oregon Schools website">
    <img class="brand-logo" src="{{ url_for('static', filename='save-oregon-schools-logo.png') }}" alt="Save Oregon Schools">
  </a>
</header>
<main>
"""

LAYOUT_END = """
</main>
<footer class="footer">
  Copyright &copy; {{ year }} Save Oregon Schools, LLC.
  <a href="https://www.saveoregonschools.com">www.saveoregonschools.com</a>
  |
  <a href="https://github.com/SaveOregonSchools">Check out all our apps on GitHub</a>
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
      <label>Preview row limit:</label>
      <input type="number" name="_limit" value="{{ (form or {}).get('_limit','500') }}" min="1" style="width:100px">
      <button type="submit">Run Query</button>
      <button formaction="{{ url_for('export') }}" formmethod="post">Export CSV (full result)</button>
    </div>

    <div class="running-msg">
      Running query. Please wait...
    </div>
  </form>

  {% if error %}
    <div class="err"><b>Error:</b>\n{{ error }}</div>
  {% endif %}

  {% if headers and rows is not none %}
    <p>Showing up to <b>{{ (form or {}).get('_limit','500') }}</b> rows. Preview contains <b>{{ len(rows) }}</b> rows.</p>

    {% if headers and headers[0] == 'generated_sql' and rows|length > 0 %}
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
    const isExport = submitter && submitter.getAttribute("formaction") === "{{ url_for('export') }}";

    const buttons = form.querySelectorAll("button");
    buttons.forEach(function(btn) {
      btn.disabled = true;
    });

    // Export CSV usually downloads a file without reloading the page,
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


def _render_query(qkey, form=None, headers=None, rows=None, error=None):
    ensure_registry()
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

    return summary, rows, updated_at, error


@app.route("/", methods=["GET"])
def home():
    return _render_home()


@app.route("/query/<qkey>", methods=["GET"])
def query_page(qkey):
    ensure_registry()
    if qkey not in REGISTRY:
        return redirect(url_for("home"))
    return _render_query(qkey, form={}, headers=None, rows=None, error=None)


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


if __name__ == "__main__":
    app.run(debug=True)
