no_cache = 1

import frappe
from frappe.utils import flt, getdate, get_first_day, get_last_day, add_months, nowdate, formatdate
from datetime import date


def get_context(context):
    context.no_breadcrumbs = True
    context.title = "Financial Reports — Apex Ledger & Audit"

    if frappe.session.user == "Guest":
        context.is_guest = True
        return

    context.is_guest = False

    # Period from query params (default: current year)
    period = frappe.form_dict.get("period", "this_year")
    company = frappe.form_dict.get("company", None)

    from_date, to_date, period_label = _resolve_period(period)
    context.period = period
    context.period_label = period_label
    context.from_date = formatdate(from_date)
    context.to_date = formatdate(to_date)

    # Company list
    companies = frappe.get_all("Company", fields=["name", "abbr", "default_currency"], order_by="name")
    context.companies = companies
    if not company and companies:
        company = companies[0].name
    context.selected_company = company

    currency = "AED"
    if company:
        co = frappe.get_value("Company", company, "default_currency")
        if co:
            currency = co
    context.currency = currency

    # ── Revenue (Sales Invoices) ──────────────────────────────
    si_filters = {"docstatus": 1, "posting_date": ["between", [from_date, to_date]]}
    if company:
        si_filters["company"] = company

    revenue_rows = frappe.db.sql("""
        SELECT SUM(base_grand_total) as total, COUNT(*) as count
        FROM `tabSales Invoice`
        WHERE docstatus=1
          AND posting_date BETWEEN %(from_date)s AND %(to_date)s
          {company_cond}
    """.format(company_cond="AND company=%(company)s" if company else ""),
        {"from_date": from_date, "to_date": to_date, "company": company},
        as_dict=True,
    )
    total_revenue = flt(revenue_rows[0].total) if revenue_rows else 0
    invoice_count = int(revenue_rows[0].count) if revenue_rows else 0

    # ── Expenses (Purchase Invoices) ─────────────────────────
    expense_rows = frappe.db.sql("""
        SELECT SUM(base_grand_total) as total, COUNT(*) as count
        FROM `tabPurchase Invoice`
        WHERE docstatus=1
          AND posting_date BETWEEN %(from_date)s AND %(to_date)s
          {company_cond}
    """.format(company_cond="AND company=%(company)s" if company else ""),
        {"from_date": from_date, "to_date": to_date, "company": company},
        as_dict=True,
    )
    total_expenses = flt(expense_rows[0].total) if expense_rows else 0
    bill_count = int(expense_rows[0].count) if expense_rows else 0

    net_profit = total_revenue - total_expenses
    profit_margin = round((net_profit / total_revenue * 100), 1) if total_revenue else 0

    context.total_revenue = _fmt(total_revenue)
    context.total_expenses = _fmt(total_expenses)
    context.net_profit = _fmt(net_profit)
    context.profit_margin = profit_margin
    context.invoice_count = invoice_count
    context.bill_count = bill_count
    context.net_profit_positive = net_profit >= 0

    # ── Outstanding Receivables ───────────────────────────────
    ar_rows = frappe.db.sql("""
        SELECT SUM(outstanding_amount) as total
        FROM `tabSales Invoice`
        WHERE docstatus=1 AND outstanding_amount > 0
          {company_cond}
    """.format(company_cond="AND company=%(company)s" if company else ""),
        {"company": company},
        as_dict=True,
    )
    context.outstanding_ar = _fmt(flt(ar_rows[0].total) if ar_rows else 0)

    # ── Monthly Revenue Trend (last 6 months) ────────────────
    context.monthly_labels = []
    context.monthly_revenue = []
    context.monthly_expenses = []
    for i in range(5, -1, -1):
        m_start = get_first_day(add_months(to_date, -i))
        m_end = get_last_day(m_start)
        label = frappe.utils.formatdate(m_start, "MMM yy")

        r = frappe.db.sql("""
            SELECT COALESCE(SUM(base_grand_total), 0) as total
            FROM `tabSales Invoice`
            WHERE docstatus=1 AND posting_date BETWEEN %(s)s AND %(e)s
            {cc}
        """.format(cc="AND company=%(company)s" if company else ""),
            {"s": m_start, "e": m_end, "company": company},
            as_dict=True,
        )
        e = frappe.db.sql("""
            SELECT COALESCE(SUM(base_grand_total), 0) as total
            FROM `tabPurchase Invoice`
            WHERE docstatus=1 AND posting_date BETWEEN %(s)s AND %(e)s
            {cc}
        """.format(cc="AND company=%(company)s" if company else ""),
            {"s": m_start, "e": m_end, "company": company},
            as_dict=True,
        )
        context.monthly_labels.append(label)
        context.monthly_revenue.append(flt(r[0].total))
        context.monthly_expenses.append(flt(e[0].total))

    # ── Top Customers ─────────────────────────────────────────
    top_customers = frappe.db.sql("""
        SELECT customer as name, SUM(base_grand_total) as total, COUNT(*) as count
        FROM `tabSales Invoice`
        WHERE docstatus=1 AND posting_date BETWEEN %(from_date)s AND %(to_date)s
          {cc}
        GROUP BY customer ORDER BY total DESC LIMIT 5
    """.format(cc="AND company=%(company)s" if company else ""),
        {"from_date": from_date, "to_date": to_date, "company": company},
        as_dict=True,
    )
    context.top_customers = top_customers
    for row in context.top_customers:
        row.total_fmt = _fmt(row.total)

    # ── Top Suppliers ─────────────────────────────────────────
    top_suppliers = frappe.db.sql("""
        SELECT supplier as name, SUM(base_grand_total) as total, COUNT(*) as count
        FROM `tabPurchase Invoice`
        WHERE docstatus=1 AND posting_date BETWEEN %(from_date)s AND %(to_date)s
          {cc}
        GROUP BY supplier ORDER BY total DESC LIMIT 5
    """.format(cc="AND company=%(company)s" if company else ""),
        {"from_date": from_date, "to_date": to_date, "company": company},
        as_dict=True,
    )
    context.top_suppliers = top_suppliers
    for row in context.top_suppliers:
        row.total_fmt = _fmt(row.total)

    # ── Recent Invoices ───────────────────────────────────────
    recent_invoices = frappe.db.sql("""
        SELECT name, customer, posting_date, base_grand_total, outstanding_amount, status
        FROM `tabSales Invoice`
        WHERE docstatus=1
          {cc}
        ORDER BY posting_date DESC LIMIT 8
    """.format(cc="AND company=%(company)s" if company else ""),
        {"company": company},
        as_dict=True,
    )
    context.recent_invoices = recent_invoices
    for row in context.recent_invoices:
        row.total_fmt = _fmt(row.base_grand_total)
        row.posting_date_fmt = formatdate(row.posting_date)

    # ── P&L breakdown by income account ──────────────────────
    income_accounts = frappe.db.sql("""
        SELECT gle.account, SUM(gle.credit - gle.debit) as amount
        FROM `tabGL Entry` gle
        JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE gle.is_cancelled = 0
          AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s
          AND acc.root_type = 'Income'
          {cc}
        GROUP BY gle.account ORDER BY amount DESC LIMIT 8
    """.format(cc="AND gle.company=%(company)s" if company else ""),
        {"from_date": from_date, "to_date": to_date, "company": company},
        as_dict=True,
    )
    context.income_accounts = income_accounts
    for row in context.income_accounts:
        row.amount_fmt = _fmt(flt(row.amount))

    expense_accounts = frappe.db.sql("""
        SELECT gle.account, SUM(gle.debit - gle.credit) as amount
        FROM `tabGL Entry` gle
        JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE gle.is_cancelled = 0
          AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s
          AND acc.root_type = 'Expense'
          {cc}
        GROUP BY gle.account ORDER BY amount DESC LIMIT 8
    """.format(cc="AND gle.company=%(company)s" if company else ""),
        {"from_date": from_date, "to_date": to_date, "company": company},
        as_dict=True,
    )
    context.expense_accounts = expense_accounts
    for row in context.expense_accounts:
        row.amount_fmt = _fmt(flt(row.amount))

    # ── Expense chart data (top 5) ────────────────────────────
    context.expense_chart_labels = [r.account.split(" - ")[0] for r in expense_accounts[:5]]
    context.expense_chart_values = [flt(r.amount) for r in expense_accounts[:5]]


def _resolve_period(period):
    today = getdate(nowdate())
    if period == "this_month":
        return get_first_day(today), get_last_day(today), "This Month"
    elif period == "last_month":
        first = get_first_day(add_months(today, -1))
        return first, get_last_day(first), "Last Month"
    elif period == "this_quarter":
        q = (today.month - 1) // 3
        start = date(today.year, q * 3 + 1, 1)
        end_month = start.month + 2
        end = get_last_day(date(start.year, end_month, 1))
        return start, end, "This Quarter"
    elif period == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), str(today.year - 1)
    else:  # this_year default
        return date(today.year, 1, 1), date(today.year, 12, 31), str(today.year)


def _fmt(amount):
    """Format number with commas, 2 decimal places."""
    return "{:,.0f}".format(flt(amount))
