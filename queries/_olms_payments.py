from __future__ import annotations

from typing import List, Tuple

from queries._olms_common import (
    add_like, clean_ein, fnums, h, mode_select, scope_clause, scope_select,
    to_float, where_sql, year_filters,
)


SUMMARY_HEADERS = [
    "f_num", "union_name", "matched_union_ein", "affiliation", "union_state",
    "period_start", "period_end", "form_type", "rpt_id", "counterparty_id",
    "payee_name", "matched_recipient_ein", "recipient_location", "type_or_class",
    "disbursement_code", "disbursement_category", "itemized_amount",
    "non_itemized_amount", "total_amount",
]

TRANSACTION_HEADERS = SUMMARY_HEADERS + ["transaction_date", "transaction_amount", "purpose"]


def headers(form):
    return TRANSACTION_HEADERS if ((form or {}).get("mode") or "summary") == "transactions" else SUMMARY_HEADERS


def render_fields(form, *, grants: bool) -> str:
    form = form or {}
    category = form.get("category", "")
    vendor_categories = [
        ("", "Default non-grant categories (501, 502, 504-506)"),
        ("all", "All disbursement categories"),
        ("501", "501 - Representational"),
        ("502", "502 - Political"),
        ("503", "503 - Contributions, gifts and grants"),
        ("504", "504 - General overhead"),
        ("505", "505 - Union administration"),
        ("506", "506 - General disbursements"),
    ]
    category_html = "" if grants else (
        "<div><label><b>Category:</b></label><br><select name='category'>"
        + "".join(
            f'<option value="{key}"{" selected" if str(category) == key else ""}>{h(label)}</option>'
            for key, label in vendor_categories
        )
        + "</select></div>"
    )
    extra = "" if grants else f"""
      <div><label><b>Type/class contains:</b></label><br><input name="type_class" value="{h(form.get('type_class',''))}"></div>
      <div><label><b>Purpose contains:</b></label><br><input name="purpose" value="{h(form.get('purpose',''))}"></div>
    """
    recipient_label = "Grantee/payee" if grants else "Vendor/payee"
    return f"""
    <div class="row" style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
      <div><label><b>Mode:</b></label><br>{mode_select(form)}</div>
      <div><label><b>Scope:</b></label><br>{scope_select(form)}</div>
      {category_html}
      <div><label><b>Minimum amount:</b></label><br><input type="number" step="any" name="min_amount" value="{h(form.get('min_amount',''))}" style="width:120px"></div>
    </div>
    <div class="row" style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
      <div><label><b>Union F_NUM(s):</b></label><br><input name="f_nums" value="{h(form.get('f_nums',''))}"></div>
      <div><label><b>Union name:</b></label><br><input name="union_name" value="{h(form.get('union_name',''))}"></div>
      <div><label><b>Matched union EIN:</b></label><br><input name="union_ein" value="{h(form.get('union_ein',''))}"></div>
      <div><label><b>Affiliation:</b></label><br><input name="affiliation" value="{h(form.get('affiliation',''))}" style="width:90px"></div>
      <div><label><b>Union state:</b></label><br><input name="union_state" maxlength="2" value="{h(form.get('union_state',''))}" style="width:65px"></div>
    </div>
    <div class="row" style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
      <div><label><b>{recipient_label} name:</b></label><br><input name="payee_name" value="{h(form.get('payee_name',''))}"></div>
      <div><label><b>Matched recipient EIN:</b></label><br><input name="recipient_ein" value="{h(form.get('recipient_ein',''))}"></div>
      <div><label><b>Recipient state:</b></label><br><input name="recipient_state" maxlength="2" value="{h(form.get('recipient_state',''))}" style="width:65px"></div>
      <div><label><b>Period year from:</b></label><br><input type="number" name="min_year" value="{h(form.get('min_year',''))}" style="width:100px"></div>
      <div><label><b>to:</b></label><br><input type="number" name="max_year" value="{h(form.get('max_year',''))}" style="width:100px"></div>
      {extra}
    </div>
    <p class="note">Summary mode uses the reported annual payee <b>TOTAL</b>. Transaction mode shows itemized rows separately; the two are never added together.</p>
    """


def build_sql(form, *, grants: bool) -> Tuple[str, List[object]]:
    form = form or {}
    transaction = (form.get("mode") or "summary") == "transactions"
    view = (
        "v_grant_transactions" if grants and transaction else
        "v_grants_paid_summary" if grants else
        "v_vendor_transactions" if transaction else
        "v_vendor_payments_summary"
    )
    clauses: List[str] = []
    params: List[object] = []
    scope_sql, scope_params = scope_clause(form.get("scope", "education"), "o")
    if scope_sql:
        clauses.append(scope_sql)
        params.extend(scope_params)
    numbers = fnums(form.get("f_nums", ""))
    if numbers:
        clauses.append("p.f_num IN (" + ",".join("?" for _ in numbers) + ")")
        params.extend(numbers)
    add_like(clauses, params, "p.union_name", form.get("union_name", ""))
    affiliation = (form.get("affiliation") or "").strip().upper()
    if affiliation:
        clauses.append("UPPER(COALESCE(p.affiliation,''))=?")
        params.append(affiliation)
    state = (form.get("union_state") or "").strip().upper()
    if state:
        clauses.append("UPPER(COALESCE(p.union_state,''))=?")
        params.append(state)
    union_ein = clean_ein(form.get("union_ein", ""))
    if union_ein:
        clauses.append("um.candidate_ein=?")
        params.append(union_ein)
    add_like(clauses, params, "p.payee_name", form.get("payee_name", ""))
    recipient_ein = clean_ein(form.get("recipient_ein", ""))
    if recipient_ein:
        clauses.append("p.matched_ein=?")
        params.append(recipient_ein)
    recipient_state = (form.get("recipient_state") or "").strip().upper()
    if recipient_state:
        clauses.append("UPPER(COALESCE(p.payee_state,''))=?")
        params.append(recipient_state)
    year_filters(clauses, params, "p.period_end", form.get("min_year"), form.get("max_year"))
    minimum = to_float(form.get("min_amount"))
    amount_column = "p.transaction_amount" if transaction else "p.total_amount"
    if minimum is not None:
        clauses.append(f"COALESCE({amount_column},0)>=?")
        params.append(minimum)
    if not grants:
        category = (form.get("category") or "").strip().lower()
        if category.isdigit():
            clauses.append("p.disbursement_code=?")
            params.append(int(category))
        elif category != "all":
            clauses.append("p.disbursement_code IN (501,502,504,505,506)")
        add_like(clauses, params, "p.type_or_class", form.get("type_class", ""))
        if transaction:
            add_like(clauses, params, "p.purpose", form.get("purpose", ""))
        elif (form.get("purpose") or "").strip():
            # Purpose only exists on itemized transactions; summary rows can be
            # constrained with an indexed relationship-preserving EXISTS.
            clauses.append(
                "EXISTS (SELECT 1 FROM v_payment_transactions t WHERE t.rpt_id=p.rpt_id "
                "AND t.payer_payee_id=p.payer_payee_id AND UPPER(COALESCE(t.purpose,'')) LIKE ?)"
            )
            params.append("%" + form.get("purpose", "").strip().upper() + "%")
    select = """
      p.f_num,p.union_name,um.candidate_ein,p.affiliation,p.union_state,
      p.period_start,p.period_end,p.form_type,p.rpt_id,p.counterparty_id,
      p.payee_name,p.matched_ein,
      TRIM(COALESCE(p.payee_city,'') || CASE WHEN p.payee_city IS NOT NULL AND p.payee_state IS NOT NULL THEN ', ' ELSE '' END || COALESCE(p.payee_state,'') || ' ' || COALESCE(p.payee_zip,'')),
      p.type_or_class,p.disbursement_code,p.disbursement_category,
      p.itemized_amount,p.non_itemized_amount,p.total_amount
    """
    if transaction:
        select += ",p.transaction_date,p.transaction_amount,p.purpose"
    sql = f"""
      SELECT {select}
      FROM {view} p
      JOIN organizations o ON o.f_num=p.f_num
      LEFT JOIN v_accepted_irs_matches um ON um.f_num=p.f_num
      {where_sql(clauses)}
      ORDER BY p.period_end DESC,{amount_column} DESC,p.union_name,p.payee_name
    """
    return sql, params
