
from flask import Flask, request, render_template_string, Response, redirect, url_for
import importlib, pkgutil, io, csv, traceback, sys, types
from pathlib import Path
from typing import Dict, Any, Tuple, List
from common import DB_PATH
from datetime import datetime

# --- Flask ---
app = Flask(__name__)

PLUGIN_PACKAGE = "queries"
PLUGIN_DIR = Path(__file__).parent / "queries"

# In-memory registry {key: module}
REGISTRY = {}

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
    global REGISTRY
    if not REGISTRY:
        REGISTRY = load_plugins()

HTML = """
<!doctype html>
<title>IRS 990 — Query Console</title>
<meta charset="utf-8">
<style>
  body { font-family: system-ui, Segoe UI, Arial; max-width: 1200px; margin: 24px auto; }
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
  .toolbar { display:flex; gap:8px; align-items:center; }
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

  body.is-running .running-msg {
    display: block;
  }

  body.is-running button {
    opacity: 0.6;
    cursor: not-allowed;
  }
  
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

</style>

<body class="{% if qkey in ['ask_database_v1', 'ask_database_v2'] %}ask-db{% endif %}">

<h1>IRS 990 — Query Console</h1>

<form method="post" action="/select">
  <div class="toolbar">
    <label for="qkey"><b>Query:</b></label>

    <select name="qkey" id="qkey"
            onchange="this.form.requestSubmit(document.getElementById('loadBtn'))">
      {% for key, mod in registry.items() %}
        <option value="{{ key }}" {% if key == qkey %}selected{% endif %}>{{ mod.META["name"] }}</option>
      {% endfor %}
    </select>

    <!-- Keep a Load button for keyboard users; we also target it from requestSubmit() -->
    <button id="loadBtn" type="submit">Load</button>

    <!-- No nested form; this button posts to /refresh -->
    <button formaction="/refresh" formmethod="post" type="submit">Refresh Queries</button>
  </div>
</form>

{% if qkey %}
  <hr>
  <h2>{{ registry[qkey].META["name"] }}</h2>
  <p>{{ registry[qkey].META.get("description","") }}</p>

  <form method="post" action="/run" onsubmit="return showRunningMessage(event, this);">
    <input type="hidden" name="qkey" value="{{ qkey }}">
    {{ registry[qkey].render_fields(form or {}) | safe }}
    <div class="toolbar">
      <label>Preview row limit:</label>
      <input type="number" name="_limit" value="{{ (form or {}).get('_limit','500') }}" min="1" style="width:100px">
      <button type="submit">Run Query</button>
      <button formaction="/export" formmethod="post">Export CSV (full result)</button>
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
    const isExport = submitter && submitter.getAttribute("formaction") === "/export";

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
</body>
"""

@app.route("/", methods=["GET"])
def home():
    ensure_registry()
    first_key = next(iter(REGISTRY.keys()), None)
    return render_template_string(HTML, registry=REGISTRY, qkey=first_key, form=None, headers=None, rows=None, error=None, len=len)

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
        qkey = next(iter(REGISTRY.keys()), None)
    return render_template_string(HTML, registry=REGISTRY, qkey=qkey, form={}, headers=None, rows=None, error=None, len=len)

@app.route("/run", methods=["GET", "POST"])
def run():
    if request.method == "GET":
        return redirect(url_for("home"))
    ensure_registry()
    qkey = request.form.get("qkey")
    form = request.form.to_dict(flat=True)
    error = None
    headers, rows = None, None
    try:
        headers, rows = REGISTRY[qkey].run(form)
        try:
            lim = max(1, int(form.get("_limit","500")))
        except:
            lim = 500
        rows = rows[:lim]
    except Exception as e:
        error = traceback.format_exc()
    return render_template_string(HTML, registry=REGISTRY, qkey=qkey, form=form, headers=headers, rows=rows, error=error, len=len)

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
    ts = datetime.now().strftime("%m-%d-%Y_%H%M")  # e.g., 08-31-2025_1053
    base = REGISTRY[qkey].META.get("key", qkey)
    filename = f"{base}_{ts}.csv"

    return Response(generate(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})

if __name__ == "__main__":
    app.run(debug=True)
