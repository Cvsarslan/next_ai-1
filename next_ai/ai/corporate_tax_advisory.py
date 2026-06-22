"""Rule-based UAE Corporate Tax (CT) intelligence for the client portal.

Two layers, mirroring the VAT advisory:

  * Company status (`get_corporate_tax_status`) — trailing financial-year revenue
    and accounting profit, whether Small Business Relief (SBR) is still available
    (revenue <= AED 3,000,000), proximity to the SBR cap, and an indicative CT
    estimate (0% up to AED 375,000 taxable income, 9% above).

  * Expense advisory (`advise_purchase_ct`) — flags transaction lines whose cost
    may be non-deductible or restricted for Corporate Tax: client entertainment
    (50% limited), fines & penalties, non-qualifying donations, interest subject
    to the deduction limitation, and personal / non-business spend.

Federal Decree-Law No. 47 of 2022. This is general guidance, not a legal opinion
or a filed return — the portal must let the user override every suggestion.
"""

import frappe
from frappe.utils import flt, cstr, getdate

# ── UAE Corporate Tax constants ──────────────────────────────────────────────
CT_ZERO_BRACKET = 375000.0     # 0% band on taxable income (Art. 3)
CT_RATE = 0.09                 # standard CT rate above the 0% band
SBR_REVENUE_CAP = 3000000.0    # Small Business Relief revenue cap (Art. 21)
# Under the published decision, SBR applies to tax periods ending on or before
# this date; verify any extension for later periods.
SBR_AVAILABLE_UNTIL = "2026-12-31"
SBR_WARN_RATIO = 0.85          # warn when revenue is within 85% of the cap

# CT deductibility classes for transaction lines.
CT_CLASSES = {
    "DEDUCTIBLE":   {"label": "Fully deductible",          "factor": 1.0},
    "ENT_50":       {"label": "Entertainment — 50% only",  "factor": 0.5},
    "NON_DEDUCT":   {"label": "Non-deductible",            "factor": 0.0},
    "INT_LIMIT":    {"label": "Interest — limitation may apply", "factor": 1.0},
    "PERSONAL":     {"label": "Non-business — non-deductible",   "factor": 0.0},
}

# Keyword → (class, message). Order matters: first match wins.
CT_KEYWORDS = [
    ("fine",          "NON_DEDUCT", "Fines and administrative penalties are not deductible for Corporate Tax (Art. 33)."),
    ("penalt",        "NON_DEDUCT", "Penalties (other than contractual compensation) are not deductible (Art. 33)."),
    ("bribe",         "NON_DEDUCT", "Bribes and illegal payments are not deductible."),
    ("dividend",      "NON_DEDUCT", "Dividends and profit distributions are not a deductible expense."),
    ("donation",      "NON_DEDUCT", "Donations/grants are only deductible if paid to a Qualifying Public Benefit Entity — otherwise add back."),
    ("charity",       "NON_DEDUCT", "Charitable contributions are deductible only to a Qualifying Public Benefit Entity — otherwise add back."),
    ("entertainment", "ENT_50",     "Client/business entertainment is only 50% deductible for Corporate Tax (Art. 32)."),
    ("hospitality",   "ENT_50",     "Hospitality/entertainment costs are restricted to 50% deductibility (Art. 32)."),
    ("restaurant",    "ENT_50",     "Meals & entertainment with clients are 50% deductible (Art. 32)."),
    ("dining",        "ENT_50",     "Client dining/entertainment is 50% deductible (Art. 32)."),
    ("gift",          "ENT_50",     "Gifts are generally treated as entertainment — 50% deductible (Art. 32)."),
    ("interest",      "INT_LIMIT",  "Net interest is subject to the 30%-of-EBITDA general interest deduction limitation (de minimis AED 12m) (Art. 30)."),
    ("finance cost",  "INT_LIMIT",  "Finance costs may be restricted by the general interest deduction limitation (Art. 30)."),
    ("loan",          "INT_LIMIT",  "Loan/interest costs may be restricted by the interest deduction limitation (Art. 30)."),
    ("personal",      "PERSONAL",   "Personal / non-business costs are not deductible for Corporate Tax."),
    ("private",       "PERSONAL",   "Private (non-business) costs are not deductible for Corporate Tax."),
]


def _items_text(items):
    parts = []
    for it in (items or []):
        for key in ("item_name", "item_code", "description", "expense_account", "account"):
            if it.get(key):
                parts.append(cstr(it[key]))
    return " ".join(parts).lower()


def _fy_bounds(company):
    """Return (start, end) of the company's current fiscal year, falling back to
    the calendar year if none is configured."""
    try:
        fy = frappe.get_all(
            "Fiscal Year",
            filters=[["year_start_date", "<=", frappe.utils.today()],
                     ["year_end_date", ">=", frappe.utils.today()]],
            fields=["year_start_date", "year_end_date"], limit=1,
        )
        if fy:
            return cstr(fy[0].year_start_date), cstr(fy[0].year_end_date)
    except Exception:
        pass
    yr = frappe.utils.today()[:4]
    return f"{yr}-01-01", f"{yr}-12-31"


# ── Company-level CT status ──────────────────────────────────────────────────
@frappe.whitelist()
def get_corporate_tax_status(company=None):
    """Revenue / SBR / indicative CT position for the current financial year."""
    try:
        if not company:
            from next_ai.api import _get_company
            company = _get_company()
        currency = frappe.get_value("Company", company, "default_currency") or "AED"
        fd, td = _fy_bounds(company)
        params = {"co": company, "fd": fd, "td": td}

        revenue = flt(frappe.db.sql(
            """SELECT IFNULL(SUM(base_net_total),0) FROM `tabSales Invoice`
               WHERE docstatus=1 AND company=%(co)s AND posting_date BETWEEN %(fd)s AND %(td)s""",
            params)[0][0])

        pl = frappe.db.sql(
            """SELECT a.root_type AS root_type,
                      IFNULL(SUM(g.credit - g.debit),0) AS credit_net,
                      IFNULL(SUM(g.debit - g.credit),0) AS debit_net
               FROM `tabGL Entry` g
               INNER JOIN `tabAccount` a ON a.name = g.account
               WHERE g.company=%(co)s AND g.is_cancelled=0
                 AND g.posting_date BETWEEN %(fd)s AND %(td)s
                 AND a.root_type IN ('Income','Expense')
               GROUP BY a.root_type""", params, as_dict=True)
        income = sum(flt(r.credit_net) for r in pl if r.root_type == "Income")
        expenses = sum(flt(r.debit_net) for r in pl if r.root_type == "Expense")
        accounting_income = round(income - expenses, 2)

        sbr_eligible = revenue <= SBR_REVENUE_CAP
        over_cap = revenue > SBR_REVENUE_CAP
        # Indicative CT (ignores SBR election and adjustments — guidance only).
        taxable = max(0.0, accounting_income)
        est_tax = round(max(0.0, taxable - CT_ZERO_BRACKET) * CT_RATE, 2)

        if over_cap:
            level, severity = "standard", "warn"
        elif revenue >= SBR_REVENUE_CAP * SBR_WARN_RATIO:
            level, severity = "approaching", "warn"
        else:
            level, severity = "sbr", "ok"

        return {
            "currency": currency,
            "period": {"from": fd, "to": td},
            "revenue": revenue,
            "accounting_income": accounting_income,
            "sbr_eligible": sbr_eligible,
            "over_sbr_cap": over_cap,
            "sbr_cap": SBR_REVENUE_CAP,
            "sbr_available_until": SBR_AVAILABLE_UNTIL,
            "zero_bracket": CT_ZERO_BRACKET,
            "rate": CT_RATE,
            "estimated_taxable_income": taxable,
            "estimated_ct": est_tax,
            "level": level,
            "severity": severity,
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Corporate Tax Advisory: status")
        return {"currency": "AED", "level": "sbr", "severity": "ok", "revenue": 0,
                "sbr_eligible": True, "over_sbr_cap": False, "sbr_cap": SBR_REVENUE_CAP}


# ── Transaction-level expense deductibility ──────────────────────────────────
@frappe.whitelist()
def advise_purchase_ct(items=None, company=None):
    """Advise Corporate Tax deductibility of an expense/purchase being drafted."""
    try:
        if isinstance(items, str):
            import json
            items = json.loads(items or "[]")
        text = _items_text(items)

        cls, message = "DEDUCTIBLE", None
        for kw, c, msg in CT_KEYWORDS:
            if kw in text:
                cls, message = c, msg
                break

        info = CT_CLASSES[cls]
        if cls == "DEDUCTIBLE":
            return _result(cls, "ok", "Deductible expense",
                           "This expense appears fully deductible for Corporate Tax, provided it is "
                           "incurred wholly and exclusively for the business and properly documented.",
                           factor=info["factor"])
        if cls == "ENT_50":
            return _result(cls, "warn", "Entertainment — 50% deductible", message,
                           factor=info["factor"],
                           notes=["Only 50% of the cost is deductible; add back the other 50% in the CT return."])
        if cls == "INT_LIMIT":
            return _result(cls, "warn", "Interest — limitation may apply", message,
                           factor=info["factor"],
                           notes=["The general interest limitation only bites above the AED 12m de minimis; "
                                  "small businesses are usually unaffected."])
        if cls == "PERSONAL":
            return _result(cls, "err", "Non-business — not deductible", message, factor=info["factor"])
        # NON_DEDUCT
        return _result(cls, "err", "Non-deductible expense", message, factor=info["factor"],
                       notes=["Add this cost back when computing taxable income."])
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Corporate Tax Advisory: purchase")
        return _result("DEDUCTIBLE", "ok", "Deductible expense",
                       "Treated as deductible by default.", factor=1.0)


def _result(cls, severity, title, reason, factor=1.0, notes=None):
    return {
        "ct_class": cls,
        "treatment": CT_CLASSES[cls]["label"],
        "deductible_factor": factor,
        "severity": severity,
        "title": title,
        "reason": reason,
        "notes": notes or [],
    }
