# queries/ask_database.py
# Natural language -> SQL generator/runner for IRS 990 database.
#
# Phase 2 behavior:
# - Takes a plain-English question.
# - Loads ai/irs990_ai_schema.md.
# - Calls Ollama on the AI server.
# - Shows the generated SQL.
# - Optionally validates and runs the SQL preview.
#
# Safety:
# - Uses read-only connect_ro().
# - Allows only SELECT / WITH queries.
# - Blocks write/admin SQL keywords.
# - Allows only approved tables/views.
# - Requires LIMIT.
# - Validates qualified column references against approved table/view schemas.
# - Includes query-complexity selector plus automatic repair for truncated or incomplete SQL.

from typing import Iterable, Tuple, List
from pathlib import Path
import os
import json
import re
import urllib.request
import urllib.error

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from common import connect_ro

def _app_root() -> Path:
    # queries/ask_database.py -> parent is queries, parent.parent is app folder
    return Path(__file__).resolve().parent.parent


if load_dotenv:
    load_dotenv(_app_root() / ".env")


META = {
    "key": "ask_database",
    "name": "Ask Database - Generate/Run SQL",
    "description": (
        "Ask a plain-English question about the IRS 990 database. "
        "Can generate SQL only, or validate and run a preview. Includes column-level validation, calculation hints, a query-complexity selector, and automatic repair for incomplete SQL."
    ),
}

HEADERS = ["question", "status", "generated_sql"]
META["headers"] = HEADERS


def _env_list(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default) or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


OLLAMA_ENDPOINTS = _env_list(
    "OLLAMA_ENDPOINTS",
    "http://localhost:11434/api/chat",
)

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "").strip()


_LAST_CACHE_KEY = None
_LAST_HEADERS = None
_LAST_ROWS = None

APPROVED_TABLES = {
    "returns",
    "canonical_by_ein_year",
    "core_hot",
    "grants_compat_v1",
    "vw_contractors",
    "officers",
    "highest_comp_employees",
    "former_key_people",
    "return_header_all",
    "sched_r_related_orgs_expanded",
}

# Approved columns by approved table/view. Used to catch model hallucinations like
# r.revenue, r.name, c.year, g.amount, etc. The validator checks qualified
# references such as r.org_name or h.total_revenue.
APPROVED_COLUMNS = {
    "returns": {
        "filing_id", "ein", "org_name", "dba_name", "return_type", "tax_year",
        "period_end", "city", "state", "zip", "website",
    },
    "canonical_by_ein_year": {
        "ein", "tax_year", "filing_id", "return_type", "return_ts",
        "amended_return_ind", "period_end",
    },
    "core_hot": {
        "filing_id", "total_revenue", "total_expenses", "net_assets_boy",
        "net_assets_eoy", "contributions", "program_service_revenue",
        "membership_dues", "investment_income", "government_grants",
        "grants_paid", "lobbying_expense", "employees_count",
        "volunteers_count", "mission_desc",
    },
    "grants_compat_v1": {
        "filing_id", "recipient_ein", "recipient_name", "city", "state",
        "country", "cash_amount", "noncash_amount", "purpose",
    },
    "vw_contractors": {
        "filing_id", "contractor_name", "business_name_line1_txt",
        "business_name_line2_txt", "person_nm", "services_desc",
        "compensation_amt", "city", "region", "country", "is_us_address",
    },
    "officers": {
        "filing_id", "person_name", "title_txt", "avg_hours_week",
        "comp_from_org", "comp_from_related", "other_compensation",
        "is_officer", "is_director", "is_key_employee", "is_former",
    },
    "highest_comp_employees": {
        "filing_id", "person_name", "title_txt", "avg_hours_week",
        "comp_from_org", "comp_from_related", "other_compensation",
    },
    "former_key_people": {
        "filing_id", "person_name", "title_txt", "comp_from_org",
        "comp_from_related", "other_compensation",
    },
    "return_header_all": {
        "filing_id", "person_nm", "preparer_person_nm", "person_title_txt",
        "signature_dt", "preparer_firm_name_business_name_line1_txt", "ptin",
        "preparation_dt", "tax_period_begin_dt", "tax_period_end_dt",
    },
    "sched_r_related_orgs_expanded": {
        "filing_id", "relationship_category", "related_ein", "related_name_line1",
        "related_name_line2", "ownership_pct", "controlled_organization_ind",
        "primary_activities_txt", "transaction_type_txt", "involved_amt",
        "exempt_code_section_txt", "public_charity_status_txt", "city_nm",
        "state_abbreviation_cd", "legal_domicile_state_cd", "country_cd",
    },
}

FORBIDDEN_KEYWORDS = {
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "replace",
    "truncate",
    "reindex",
    "begin",
    "commit",
    "rollback",
}


def _html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _app_root() -> Path:
    # queries/ask_database_v2.py -> parent is queries, parent.parent is app folder
    return Path(__file__).resolve().parent.parent


def _schema_path() -> Path:
    return _app_root() / "ai" / "irs990_ai_schema.md"


def _load_schema_guide() -> str:
    path = _schema_path()
    if not path.exists():
        raise FileNotFoundError(f"Schema guide not found: {path}")
    return path.read_text(encoding="utf-8")


DEFAULT_COMPLEXITY_OPTIONS = {
    "standard": {
        "label": "Standard — faster (8K context, 1000-token output)",
        "description": "Best for normal lookups, filters, rankings, and most single-step questions.",
        "num_ctx": 8192,
        "num_predict": 1000,
        "timeout": 180,
    },
}


def _resolve_app_path(path_text: str) -> Path:
    p = Path(path_text).expanduser()
    if p.is_absolute():
        return p
    return (_app_root() / p).resolve()


def _load_complexity_config() -> tuple[dict, str]:
    config_path_text = (os.getenv("OLLAMA_COMPLEXITY_CONFIG") or "").strip()

    if not config_path_text:
        return DEFAULT_COMPLEXITY_OPTIONS, "standard"

    config_path = _resolve_app_path(config_path_text)

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Could not load Ollama complexity config at {config_path}; using defaults. Error: {e}")
        return DEFAULT_COMPLEXITY_OPTIONS, "standard"

    options = data.get("options")
    default_key = str(data.get("default") or "standard").strip().lower()

    if not isinstance(options, dict) or not options:
        print(f"Ollama complexity config at {config_path} has no valid options; using defaults.")
        return DEFAULT_COMPLEXITY_OPTIONS, "standard"

    cleaned = {}

    for key, cfg in options.items():
        if not isinstance(cfg, dict):
            continue

        key = str(key).strip().lower()
        if not key:
            continue

        try:
            cleaned[key] = {
                "label": str(cfg.get("label") or key.title()),
                "description": str(cfg.get("description") or ""),
                "num_ctx": int(cfg.get("num_ctx")),
                "num_predict": int(cfg.get("num_predict")),
                "timeout": int(cfg.get("timeout")),
            }
        except Exception as e:
            print(f"Skipping invalid complexity option '{key}' in {config_path}: {e}")

    if not cleaned:
        return DEFAULT_COMPLEXITY_OPTIONS, "standard"

    if default_key not in cleaned:
        default_key = next(iter(cleaned.keys()))

    return cleaned, default_key


COMPLEXITY_OPTIONS, DEFAULT_COMPLEXITY = _load_complexity_config()


def _normalize_complexity(value: str) -> str:
    value = (value or DEFAULT_COMPLEXITY).strip().lower()
    return value if value in COMPLEXITY_OPTIONS else DEFAULT_COMPLEXITY


def _ollama_options_for_complexity(complexity: str) -> dict:
    """
    Return Ollama generation settings for the selected query complexity.
    Environment variables still override the UI choice if you need to force
    a setting while testing:
      OLLAMA_NUM_CTX
      OLLAMA_NUM_PREDICT
      OLLAMA_TIMEOUT
    """
    c = COMPLEXITY_OPTIONS[_normalize_complexity(complexity)]
    return {
        "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", str(c["num_ctx"]))),
        "num_predict": int(os.getenv("OLLAMA_NUM_PREDICT", str(c["num_predict"]))),
        "timeout": int(os.getenv("OLLAMA_TIMEOUT", str(c["timeout"]))),
    }

def render_fields(form) -> str:
    f = form or {}
    question = f.get("question", "")
    model = (f.get("model", OLLAMA_MODEL) or "").strip()
    generate_only = f.get("generate_only") in (True, "true", "on", "1")
    sql_mode = f.get("sql_mode", "generate")
    manual_sql = f.get("manual_sql", "")
    complexity = _normalize_complexity(f.get("query_complexity", DEFAULT_COMPLEXITY))
    complexity_options_html = "\n".join(
        f'<option value="{key}" {"selected" if key == complexity else ""}>{_html(cfg["label"])}</option>'
        for key, cfg in COMPLEXITY_OPTIONS.items()
    )
    complexity_help = _html(COMPLEXITY_OPTIONS[complexity]["description"])

    return f"""
    <div class="row" style="display:flex; gap:20px; align-items:center; flex-wrap:wrap;">
      <label>
        <input type="radio" name="sql_mode" value="generate" {"checked" if sql_mode != "manual" else ""}>
        <b>Ask question / generate SQL</b>
      </label>

      <label>
        <input type="radio" name="sql_mode" value="manual" {"checked" if sql_mode == "manual" else ""}>
        <b>Run existing SQL</b>
      </label>
    </div>

    <div class="row">
      <label for="question"><b>Ask a database question:</b></label><br>
      <textarea id="question" name="question" rows="5"
        placeholder="Example: Show Oregon nonprofits with more than $1 million in government grants in 2022.">{_html(question)}</textarea>
      <div style="color:#666; font-size:90%; margin-top:4px;">
        Used when “Ask question / generate SQL” is selected. By default, the app generates SQL and runs a validated preview.
      </div>
    </div>

    <div class="row">
      <label for="manual_sql"><b>Existing SQL to validate and run:</b></label><br>
      <textarea id="manual_sql" name="manual_sql" rows="10"
        placeholder="Paste a SELECT query here. It must use approved tables/views and include LIMIT.">{_html(manual_sql)}</textarea>
      <div style="color:#666; font-size:90%; margin-top:4px;">
        Used when “Run existing SQL” is selected. This does not call Ollama.
      </div>
    </div>

    <div class="row" style="display:flex; gap:16px; flex-wrap:wrap; align-items:center;">
      <label>
        <b>Ollama model:</b>
        <input type="text" name="model" value="{_html(model)}" style="width:260px;">
          <div style="color:#666; font-size:90%; margin-top:4px;">
            Leave blank to use the model configured in .env as OLLAMA_MODEL.
          </div>
      </label>

      <label>
        <b>Query complexity:</b>
        <select name="query_complexity" style="min-width:260px;">
          {complexity_options_html}
        </select>
      </label>

      <label>
        <input type="checkbox" name="generate_only" {"checked" if generate_only else ""}>
        <b>Generate SQL only — do not run preview</b>
      </label>
    </div>

    <div class="row" style="color:#666; font-size:90%;">
      Default behavior: generate SQL, validate it, and run a preview.
      Safety rules: SELECT/WITH only, approved tables only, LIMIT required.
      For “Run existing SQL,” the SQL is always validated and run directly.<br>
      Selected complexity: {complexity_help}
    </div>
    """



def _question_hints(question: str) -> str:
    """
    Add narrow, task-specific hints for common questions where small local models
    often hallucinate columns or choose the wrong SQL shape.
    """
    q = (question or "").lower()
    hints = []

    if any(w in q for w in ("growth", "grew", "increase", "decrease", "change", "from", "between")):
        hints.append("""
For multi-year comparisons, do not invent growth columns. Build one CTE per year from canonical_by_ein_year + returns + core_hot, then join the CTEs on EIN. For revenue growth, use h.total_revenue. For expense growth, use h.total_expenses. For grant growth, use h.grants_paid or h.government_grants depending on the question.
""".strip())

    if any(w in q for w in ("percentage", "percent", "%", "pct")):
        hints.append("""
For percent calculations, use safe division with NULLIF(denominator,0), and usually ROUND(100.0 * numerator / NULLIF(denominator,0), 2).
""".strip())

    if any(w in q for w in ("top", "largest", "highest", "biggest")):
        hints.append("""
For top-N questions, order by the calculated measure DESC and include a numeric LIMIT matching the requested count, or LIMIT 500 if no count is specified.
""".strip())

    if not hints:
        return ""

    return "\n\nTASK-SPECIFIC HINTS:\n" + "\n".join(f"- {h}" for h in hints)

def _build_prompt(schema_guide: str, question: str) -> List[dict]:
    hints = _question_hints(question)

    system_msg = f"""
You are an expert SQLite SQL generator for an IRS 990 research database.

Your job:
- Convert the user's plain-English question into one SQLite SELECT query.
- Return SQL only.
- Do not explain the SQL.
- Do not wrap the SQL in markdown.
- Do not include backticks.
- Follow the schema guide exactly.
- Always include LIMIT 500 unless the user asks for fewer rows.
- Return a complete query. Do not stop mid-query. The final query must include ORDER BY when needed and a numeric LIMIT.

SCHEMA GUIDE:
{schema_guide}
""".strip()

    user_msg = f"""
Question:
{question}{hints}

Return only the complete SQLite SQL query. Make sure it ends with a numeric LIMIT.
""".strip()

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]


def _extract_ollama_content(data: dict) -> str:
    """
    Extract assistant text from Ollama /api/chat response variants.
    Normal response shape is {"message": {"content": "..."}}.
    This also tolerates legacy-ish shapes so an unusual response does not look empty.
    """
    if not isinstance(data, dict):
        return ""
    msg = data.get("message") or {}
    if isinstance(msg, dict):
        content = msg.get("content") or ""
        if content:
            return str(content).strip()
    for key in ("response", "content", "text"):
        val = data.get(key)
        if val:
            return str(val).strip()
    return ""


def _ollama_empty_detail(data: dict) -> str:
    """Small diagnostic string for empty Ollama responses."""
    if not isinstance(data, dict):
        return "non-JSON response object"
    bits = []
    for k in ("model", "done", "done_reason", "total_duration", "load_duration", "prompt_eval_count", "eval_count"):
        if k in data:
            bits.append(f"{k}={data.get(k)}")
    msg = data.get("message")
    if isinstance(msg, dict):
        bits.append("message_keys=" + ",".join(sorted(msg.keys())))
    return "; ".join(bits) if bits else "no diagnostic fields returned"

def _call_ollama(question: str, model: str, complexity: str = "standard") -> Tuple[str, str]:
    schema_guide = _load_schema_guide()
    messages = _build_prompt(schema_guide, question)

    ollama_opts = _ollama_options_for_complexity(complexity)

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0.1,
            "top_p": 0.9,
            "num_ctx": ollama_opts["num_ctx"],
            "num_predict": ollama_opts["num_predict"],
        },
    }

    body = json.dumps(payload).encode("utf-8")
    errors = []

    for endpoint in OLLAMA_ENDPOINTS:
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=ollama_opts["timeout"]) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                content = _extract_ollama_content(data)

                if not content:
                    # Do not stop on the first empty endpoint. Record diagnostics and
                    # try the LAN fallback before returning an error.
                    errors.append(f"{endpoint}: empty response ({_ollama_empty_detail(data)})")
                    continue

                return "ok", _clean_sql(content)

        except urllib.error.URLError as e:
            errors.append(f"{endpoint}: {e}")
        except TimeoutError as e:
            errors.append(f"{endpoint}: timeout: {e}")
        except Exception as e:
            errors.append(f"{endpoint}: {type(e).__name__}: {e}")

    return "error", "Could not get a usable response from Ollama.\n\n" + "\n".join(errors)

def _repair_sql_with_ollama(question: str, bad_sql: str, error_message: str, model: str, complexity: str = "standard") -> Tuple[str, str]:
    """
    One-shot SQL repair.
    Sends the original question, failed SQL, SQLite error, and schema guide back to Ollama.
    Returns (status, repaired_sql_or_error).
    """
    schema_guide = _load_schema_guide()

    system_msg = f"""
You are an expert SQLite SQL repair assistant for an IRS 990 research database.

Your job:
- Fix the SQL query so it runs in SQLite.
- Return SQL only.
- Do not explain the SQL.
- Do not wrap the SQL in markdown.
- Do not include backticks.
- Use only the schema guide below.
- Only return one SELECT or WITH query.
- Always include LIMIT 500 unless the original question asks for fewer rows.
- Return a complete query. Do not stop mid-query. The final query must include a numeric LIMIT.

SCHEMA GUIDE:
{schema_guide}
""".strip()

    user_msg = f"""
Original user question:
{question}

The SQL below failed:

{bad_sql}

SQLite error:
{error_message}

Return only the corrected SQLite SQL query.
""".strip()

    ollama_opts = _ollama_options_for_complexity(complexity)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0.05,
            "num_ctx": ollama_opts["num_ctx"],
            "num_predict": ollama_opts["num_predict"],
            "top_p": 0.9,
        },
    }

    body = json.dumps(payload).encode("utf-8")
    errors = []

    for endpoint in OLLAMA_ENDPOINTS:
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=ollama_opts["timeout"]) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                content = _extract_ollama_content(data)

                if not content:
                    errors.append(f"{endpoint}: empty repair response ({_ollama_empty_detail(data)})")
                    continue

                return "ok", _clean_sql(content)

        except urllib.error.URLError as e:
            errors.append(f"{endpoint}: {e}")
        except TimeoutError as e:
            errors.append(f"{endpoint}: timeout: {e}")
        except Exception as e:
            errors.append(f"{endpoint}: {type(e).__name__}: {e}")

    return "error", "Could not get a usable SQL repair response from Ollama.\n\n" + "\n".join(errors)

def _clean_sql(text: str) -> str:
    """
    Clean common model formatting mistakes.
    """
    s = (text or "").strip()

    # Remove markdown fences if the model disobeys.
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()

    # Remove a leading sql language tag if present.
    if s.lower().startswith("sql\n"):
        s = s[4:].strip()

    return s.strip()


def _strip_sql_comments(sql: str) -> str:
    # Remove -- line comments and /* block comments */
    s = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    return s.strip()


def _extract_cte_names(cleaned_sql: str) -> set:
    """
    Best-effort CTE extraction. This is intentionally simple but works for the
    common model output pattern: WITH cte_name AS (...), cte2 AS (...)
    """
    ctes = set()
    s = cleaned_sql.strip()
    if not s.lower().startswith("with"):
        return ctes

    # Match names followed by AS ( near the WITH list. This may over-capture in
    # unusual nested cases, but it is safe because CTEs are only used to avoid
    # falsely blocking references in FROM/JOIN.
    for m in re.finditer(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(", s, flags=re.IGNORECASE):
        name = m.group(1).lower()
        if name not in {"select", "from", "join", "where", "case"}:
            ctes.add(name)
    return ctes


def _strip_string_literals(sql: str) -> str:
    # Replace single-quoted SQL string literals so table/column regexes do not
    # accidentally inspect text inside LIKE '%SOMETHING%'. Handles doubled quotes.
    return re.sub(r"'(?:''|[^'])*'", "''", sql)


def _extract_table_refs(cleaned_sql: str) -> List[Tuple[str, str]]:
    """
    Return [(table_or_cte, alias_or_table)] for simple FROM/JOIN refs.
    Examples:
      FROM returns r              -> (returns, r)
      JOIN core_hot AS h          -> (core_hot, h)
      FROM rev_2024               -> (rev_2024, rev_2024)
    """
    refs = []
    no_strings = _strip_string_literals(cleaned_sql)
    pattern = re.compile(
        r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)"
        r"(?:\s+(?:AS\s+)?([a-zA-Z_][a-zA-Z0-9_]*))?",
        flags=re.IGNORECASE,
    )
    reserved = {
        "on", "where", "join", "left", "right", "inner", "outer", "full",
        "cross", "group", "order", "limit", "union", "having",
    }
    for table, alias in pattern.findall(no_strings):
        table_l = table.lower()
        alias_l = (alias or table).lower()
        if alias_l in reserved:
            alias_l = table_l
        refs.append((table_l, alias_l))
    return refs


def _validate_known_columns(cleaned_sql: str, cte_names: set) -> Tuple[bool, str]:
    """
    Validate qualified column references for approved base tables/views.
    This intentionally does not validate columns from CTE aliases, because their
    output columns are query-defined. It catches the common dangerous errors:
    r.revenue, r.name, c.year, h.revenue, g.amount, etc.
    """
    refs = _extract_table_refs(cleaned_sql)
    alias_to_table = {}

    for table, alias in refs:
        if table in APPROVED_TABLES:
            alias_to_table[alias] = table
            alias_to_table[table] = table
        elif table in cte_names:
            # CTE aliases are allowed but column definitions are query-local.
            alias_to_table[alias] = None
            alias_to_table[table] = None

    no_strings = _strip_string_literals(cleaned_sql)
    problems = []

    for alias, col in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\b", no_strings):
        alias_l = alias.lower()
        col_l = col.lower()

        if alias_l not in alias_to_table:
            # Could be a CTE/table alias from a more complex subquery. Let SQLite
            # handle it rather than over-blocking otherwise valid SQL.
            continue

        table = alias_to_table[alias_l]
        if table is None:
            continue

        if col_l not in APPROVED_COLUMNS.get(table, set()):
            problems.append(f"{alias}.{col} is not a column in {table}")

    if problems:
        return False, "Unknown or non-approved column reference(s): " + "; ".join(sorted(set(problems)))

    return True, "Column references passed validation."


def _validate_sql(sql: str) -> Tuple[bool, str]:
    """
    Conservative safety validator plus v2 column validation.
    """
    if not sql or not sql.strip():
        return False, "No SQL was generated."

    cleaned = _strip_sql_comments(sql)
    lowered = cleaned.lower().strip()

    # Allow one trailing semicolon, but block multiple statements.
    no_trailing = lowered[:-1].strip() if lowered.endswith(";") else lowered
    if ";" in no_trailing:
        return False, "Multiple SQL statements are not allowed."

    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False, "Only SELECT or WITH queries are allowed."

    # Keyword block.
    tokens = set(re.findall(r"\b[a-z_]+\b", lowered))
    bad = sorted(tokens.intersection(FORBIDDEN_KEYWORDS))
    if bad:
        return False, f"Forbidden SQL keyword found: {', '.join(bad)}"

    if not re.search(r"\blimit\s+\d+\b", lowered):
        return False, "Generated SQL must include a numeric LIMIT."

    cte_names = _extract_cte_names(cleaned)
    refs = _extract_table_refs(cleaned)
    refs_lower = {table for table, alias in refs}

    unknown = sorted(t for t in refs_lower if t not in APPROVED_TABLES and t not in cte_names)
    if unknown:
        return False, "SQL references non-approved table/view(s): " + ", ".join(unknown)

    if not refs_lower:
        return False, "No approved table/view references were found."

    ok_cols, col_msg = _validate_known_columns(cleaned, cte_names)
    if not ok_cols:
        return False, col_msg

    return True, "SQL passed validation."


def _run_sql_preview(sql: str) -> Tuple[List[str], List[Tuple]]:
    conn = connect_ro()
    cur = conn.execute(sql)
    headers = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    return headers, rows


def _generate_only(question: str, model: str, complexity: str = "standard") -> Tuple[List[str], List[Tuple]]:
    status, generated = _call_ollama(question, model, complexity)
    return HEADERS, [(question, status, generated)]


def _looks_truncated_sql(sql: str) -> bool:
    """
    Best-effort detection for SQL that was cut off by the model before it completed.
    This catches cases like ending in "ON r18.ein = r24" or having unbalanced
    parentheses. It is intentionally conservative; valid SQL still goes through
    normal validation/execution.
    """
    s = _strip_sql_comments(sql or "").strip()
    if not s:
        return True

    lowered = s.lower().rstrip("; \n\t")

    # Common dangling endings when generation is cut off.
    dangling_patterns = [
        r"\b(on|where|and|or|join|from|order\s+by|group\s+by|having|limit)\s*$",
        r"[.=,+\-*/(]\s*$",
        r"\b[a-zA-Z_][a-zA-Z0-9_]*\.$",
    ]
    if any(re.search(p, lowered, flags=re.IGNORECASE) for p in dangling_patterns):
        return True

    # Parentheses balance outside string literals.
    no_strings = _strip_string_literals(s)
    if no_strings.count("(") != no_strings.count(")"):
        return True

    # WITH queries need a final SELECT after the CTE declarations.
    if lowered.startswith("with") and not re.search(r"\)\s*select\b|\),\s*[a-zA-Z_][a-zA-Z0-9_]*\s+as\s*\(", lowered, flags=re.IGNORECASE):
        # This is a weak signal, so only call it truncated if it also lacks LIMIT.
        if not re.search(r"\blimit\s+\d+\b", lowered):
            return True

    return False


def _repair_or_return_validation_error(question: str, generated: str, validation_message: str, model: str, complexity: str) -> Tuple[str, str, str]:
    """
    For invalid generated SQL, try one repair pass instead of immediately showing
    a validation error. This is especially useful when Ollama truncates output
    before the LIMIT or final JOIN condition is complete.

    Returns (status, sql_or_error, note).
    status is "ok" when repaired SQL passed validation.
    """
    repair_context = validation_message
    if _looks_truncated_sql(generated):
        repair_context = (
            "The generated SQL appears incomplete or truncated. Finish the query, "
            "repair any dangling JOIN/ON/WHERE clauses, and include a numeric LIMIT. "
            f"Validator message: {validation_message}"
        )

    repair_status, repaired = _repair_sql_with_ollama(
        question=question,
        bad_sql=generated,
        error_message=repair_context,
        model=model,
        complexity=complexity,
    )

    if repair_status != "ok":
        return (
            "error",
            f"Generated SQL failed validation:\n{validation_message}\n\n"
            f"Generated SQL:\n{generated}\n\n"
            f"Repair failure:\n{repaired}",
            "",
        )

    valid2, message2 = _validate_sql(repaired)
    if not valid2:
        return (
            "error",
            f"Generated SQL failed validation:\n{validation_message}\n\n"
            f"Generated SQL:\n{generated}\n\n"
            f"Repaired SQL failed validation:\n{message2}\n\n"
            f"Repaired SQL:\n{repaired}",
            "",
        )

    note = (
        "-- NOTE: The original generated SQL failed validation and was repaired once.\n"
        f"-- Original validation message: {validation_message}\n\n"
    )
    return "ok", repaired, note


def _generate_and_run(question: str, model: str, complexity: str = "standard") -> Tuple[List[str], List[Tuple]]:
    status, generated = _call_ollama(question, model, complexity)

    if status != "ok":
        return HEADERS, [(question, status, generated)]

    valid, message = _validate_sql(generated)
    if not valid:
        repair_status, repaired_or_error, repair_note = _repair_or_return_validation_error(
            question=question,
            generated=generated,
            validation_message=message,
            model=model,
            complexity=complexity,
        )
        if repair_status != "ok":
            return HEADERS, [(question, "validation_error_repair_failed", repaired_or_error)]
        generated = repaired_or_error

    try:
        result_headers, result_rows = _run_sql_preview(generated)
        final_sql = generated
        repair_note = locals().get("repair_note", "")
    except Exception as e:
        first_error = f"{type(e).__name__}: {e}"

        # One repair attempt only.
        repair_status, repaired = _repair_sql_with_ollama(
            question=question,
            bad_sql=generated,
            error_message=first_error,
            model=model,
            complexity=complexity,
        )

        if repair_status != "ok":
            return HEADERS, [
                (
                    question,
                    "sql_error_repair_failed",
                    f"Original SQL error:\n{first_error}\n\n"
                    f"Original generated SQL:\n{generated}\n\n"
                    f"Repair failure:\n{repaired}",
                )
            ]

        valid2, message2 = _validate_sql(repaired)
        if not valid2:
            return HEADERS, [
                (
                    question,
                    "repair_validation_error",
                    f"Original SQL error:\n{first_error}\n\n"
                    f"Original generated SQL:\n{generated}\n\n"
                    f"Repaired SQL failed validation:\n{message2}\n\n"
                    f"Repaired SQL:\n{repaired}",
                )
            ]

        try:
            result_headers, result_rows = _run_sql_preview(repaired)
            final_sql = repaired
            repair_note = (
                "-- NOTE: The original generated SQL failed and was repaired once.\n"
                f"-- Original SQLite error: {first_error}\n\n"
            )
        except Exception as e2:
            second_error = f"{type(e2).__name__}: {e2}"
            return HEADERS, [
                (
                    question,
                    "sql_error_after_repair",
                    f"Original SQL error:\n{first_error}\n\n"
                    f"Original generated SQL:\n{generated}\n\n"
                    f"Repaired SQL error:\n{second_error}\n\n"
                    f"Repaired SQL:\n{repaired}",
                )
            ]

    # For run mode, return the actual query results.
    # Add a first column showing the final generated/repaired SQL so app.py can display it once.
    headers = ["generated_sql"] + result_headers
    display_sql = repair_note + final_sql

    if not result_rows:
        return headers, [(display_sql, *["" for _ in result_headers])]

    rows = [(display_sql, *row) for row in result_rows]
    return headers, rows


def _cache_key(form) -> tuple:
    f = form or {}
    sql_mode = f.get("sql_mode", "generate")

    return (
        sql_mode,
        (f.get("question", "") or "").strip(),
        (f.get("manual_sql", "") or "").strip(),
        (f.get("model", OLLAMA_MODEL) or OLLAMA_MODEL).strip(),
        _normalize_complexity(f.get("query_complexity", DEFAULT_COMPLEXITY)),
        f.get("generate_only") in (True, "true", "on", "1"),
    )


def _run_manual_sql(sql: str) -> Tuple[List[str], List[Tuple]]:
    sql = (sql or "").strip()

    if not sql:
        return HEADERS, [("", "error", "Please paste a SQL query.")]

    valid, message = _validate_sql(sql)
    if not valid:
        return HEADERS, [("", "validation_error", message + "\n\nSQL:\n" + sql)]

    try:
        result_headers, result_rows = _run_sql_preview(sql)
    except Exception as e:
        return HEADERS, [("", "sql_error", f"{type(e).__name__}: {e}\n\nSQL:\n{sql}")]

    headers = ["generated_sql"] + result_headers

    if not result_rows:
        return headers, [(sql, *["" for _ in result_headers])]

    return headers, [(sql, *row) for row in result_rows]


def run(form):
    global _LAST_CACHE_KEY, _LAST_HEADERS, _LAST_ROWS

    f = form or {}
    sql_mode = f.get("sql_mode", "generate")
    question = (f.get("question", "") or "").strip()
    manual_sql = (f.get("manual_sql", "") or "").strip()
    model = (f.get("model") or OLLAMA_MODEL or "").strip()
    complexity = _normalize_complexity(f.get("query_complexity", DEFAULT_COMPLEXITY))
    generate_only = f.get("generate_only") in (True, "true", "on", "1")

    key = _cache_key(form)

    if _LAST_CACHE_KEY == key and _LAST_HEADERS is not None and _LAST_ROWS is not None:
        return _LAST_HEADERS, _LAST_ROWS

    if sql_mode == "manual":
        headers, rows = _run_manual_sql(manual_sql)
    else:
        if not question:
            headers, rows = HEADERS, [("", "error", "Please enter a question.")]
        elif not model:
            headers, rows = HEADERS, [
                (
                    question,
                    "error",
                    "No Ollama model was provided. Enter a model in the form or set OLLAMA_MODEL in .env."
                )
            ]
        elif generate_only:
            headers, rows = _generate_only(question, model, complexity)
        else:
            headers, rows = _generate_and_run(question, model, complexity)

    _LAST_CACHE_KEY = key
    _LAST_HEADERS = headers
    _LAST_ROWS = rows

    return headers, rows
    
    
def export_headers(form):
    global _LAST_CACHE_KEY, _LAST_HEADERS, _LAST_ROWS

    key = _cache_key(form)

    if _LAST_CACHE_KEY == key and _LAST_HEADERS is not None:
        headers = _LAST_HEADERS
    else:
        headers, rows = run(form)

    # Do not export the generated SQL helper column.
    if headers and len(headers) > 0 and headers[0] == "generated_sql":
        return headers[1:]

    return headers

def export_rows(form) -> Iterable[Tuple]:
    global _LAST_CACHE_KEY, _LAST_HEADERS, _LAST_ROWS

    key = _cache_key(form)

    if _LAST_CACHE_KEY == key and _LAST_ROWS is not None:
        rows = _LAST_ROWS
        headers = _LAST_HEADERS
    else:
        headers, rows = run(form)

    # Do not export the generated SQL helper column.
    if headers and len(headers) > 0 and headers[0] == "generated_sql":
        return [tuple(row[1:]) for row in rows]

    return rows