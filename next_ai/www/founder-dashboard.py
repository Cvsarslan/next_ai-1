no_cache = 1

import frappe
from frappe.utils import flt, getdate, get_first_day, get_last_day, add_months, nowdate, formatdate
from datetime import date


def _set_defaults(context):
    """Set safe fallback values so the template never hits UndefinedError."""
    context.currency = "AED"
    context.period_label = ""
    context.from_date = ""
    context.to_date = ""
    context.today = ""
    context.companies = []
    context.selected_company = ""
    # Financials
    context.sales_total = "0"
    context.sales_count = 0
    context.purchase_total = "0"
    context.purchase_count = 0
    context.net_profit = "0"
    context.net_profit_raw = 0
    context.net_profit_positive = True
    context.incoming_payment = "0"
    context.outgoing_payment = "0"
    context.outstanding_ar = "0"
    context.outstanding_ap = "0"
    context.active_customers = 0
    context.so_to_bill = 0
    context.trend_labels = []
    context.trend_sales = []
    context.trend_purchase = []
    # HR
    context.total_employees = 0
    context.job_openings = 0
    context.new_hires = 0
    context.separations = 0
    context.att_present = 0
    context.att_absent = 0
    context.att_half_day = 0
    context.att_on_leave = 0
    context.att_wfh = 0
    context.att_holiday = 0
    context.att_total = 0
    context.att_labels = ["Present", "Absent", "Half Day", "On Leave", "Work From Home", "Holiday"]
    context.att_values = [0, 0, 0, 0, 0, 0]
    context.expense_claims_amt = "0"
    context.expense_claims_count = 0
    context.salary_total = "0"
    context.salary_count = 0
    context.leave_count = 0
    # CRM
    context.leads = []
    context.total_leads = 0
    context.lead_labels = []
    context.lead_values = []
    context.leads_open = 0
    context.leads_converted = 0
    context.leads_interested = 0
    context.leads_lost = 0
    context.quotations = []
    context.total_quotations = 0
    context.quote_labels = []
    context.quote_values = []
    context.opportunities = []
    context.total_opportunities = 0
    context.opp_open = 0
    context.opp_pipeline_val = "0"
    context.recent_leads = []
    # Assets
    context.total_assets = 0
    context.asset_value = "0"
    context.new_assets = 0
    context.asset_categories = []
    context.asset_cat_labels = []
    context.asset_cat_values = []
    # Projects
    context.project_statuses = []
    context.total_projects = 0
    context.proj_open = 0
    context.proj_completed = 0
    context.proj_cancelled = 0
    context.overdue_tasks = 0
    context.proj_labels = []
    context.proj_values = []
    context.recent_projects = []


def get_context(context):
    context.no_breadcrumbs = True
    context.title = "Founder Dashboard"

    if frappe.session.user == "Guest":
        context.is_guest = True
        return

    context.is_guest = False
    _set_defaults(context)

    period = frappe.form_dict.get("period", "this_year")
    company = frappe.form_dict.get("company") or None

    from_date, to_date, period_label = _resolve_period(period)
    context.period = period
    context.period_label = period_label
    context.from_date = formatdate(from_date)
    context.to_date = formatdate(to_date)
    context.today = formatdate(getdate(nowdate()))

    # Company list
    companies = frappe.get_all("Company", fields=["name", "default_currency"], order_by="name")
    context.companies = companies
    if not company and companies:
        company = companies[0].name
    context.selected_company = company

    currency = "AED"
    if company:
        cur = frappe.get_value("Company", company, "default_currency")
        if cur:
            currency = cur
    context.currency = currency

    cc = "AND company=%(company)s" if company else ""
    # Aliased variants to avoid ambiguous column errors when ERPNext adds permission JOINs
    cc_si  = "AND si.company=%(company)s"  if company else ""
    cc_pi  = "AND pi.company=%(company)s"  if company else ""
    cc_pe  = "AND pe.company=%(company)s"  if company else ""
    cc_so  = "AND so.company=%(company)s"  if company else ""
    cc_q   = "AND q.company=%(company)s"   if company else ""
    params = {"from_date": from_date, "to_date": to_date, "company": company}

    # ═══════════════════════════════════════════
    # FINANCIALS
    # ═══════════════════════════════════════════

    # Sales (period)
    si = frappe.db.sql("""
        SELECT COALESCE(SUM(si.base_grand_total),0) as total, COUNT(*) as cnt
        FROM `tabSales Invoice` si
        WHERE si.docstatus=1 AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s {cc}
    """.format(cc=cc_si), params, as_dict=True)
    context.sales_total = _fmt(flt(si[0].total) if si else 0)
    context.sales_count = int(si[0].cnt) if si else 0

    # Purchase (period)
    pi = frappe.db.sql("""
        SELECT COALESCE(SUM(pi.base_grand_total),0) as total, COUNT(*) as cnt
        FROM `tabPurchase Invoice` pi
        WHERE pi.docstatus=1 AND pi.posting_date BETWEEN %(from_date)s AND %(to_date)s {cc}
    """.format(cc=cc_pi), params, as_dict=True)
    context.purchase_total = _fmt(flt(pi[0].total) if pi else 0)
    context.purchase_count = int(pi[0].cnt) if pi else 0

    context.net_profit_raw = flt(si[0].total if si else 0) - flt(pi[0].total if pi else 0)
    context.net_profit = _fmt(context.net_profit_raw)
    context.net_profit_positive = context.net_profit_raw >= 0

    # Incoming payments (period)
    pay_in = frappe.db.sql("""
        SELECT COALESCE(SUM(pe.paid_amount),0) as total, COUNT(*) as cnt
        FROM `tabPayment Entry` pe
        WHERE pe.docstatus=1 AND pe.payment_type='Receive'
          AND pe.posting_date BETWEEN %(from_date)s AND %(to_date)s {cc}
    """.format(cc=cc_pe), params, as_dict=True)
    context.incoming_payment = _fmt(flt(pay_in[0].total) if pay_in else 0)

    # Outgoing payments (period)
    pay_out = frappe.db.sql("""
        SELECT COALESCE(SUM(pe.paid_amount),0) as total, COUNT(*) as cnt
        FROM `tabPayment Entry` pe
        WHERE pe.docstatus=1 AND pe.payment_type='Pay'
          AND pe.posting_date BETWEEN %(from_date)s AND %(to_date)s {cc}
    """.format(cc=cc_pe), params, as_dict=True)
    context.outgoing_payment = _fmt(flt(pay_out[0].total) if pay_out else 0)

    # Outstanding receivable (all time)
    ar = frappe.db.sql("""
        SELECT COALESCE(SUM(si.outstanding_amount),0) as total
        FROM `tabSales Invoice` si
        WHERE si.docstatus=1 AND si.outstanding_amount>0 {cc}
    """.format(cc=cc_si), params, as_dict=True)
    context.outstanding_ar = _fmt(flt(ar[0].total) if ar else 0)

    # Outstanding payable
    ap = frappe.db.sql("""
        SELECT COALESCE(SUM(pi.outstanding_amount),0) as total
        FROM `tabPurchase Invoice` pi
        WHERE pi.docstatus=1 AND pi.outstanding_amount>0 {cc}
    """.format(cc=cc_pi), params, as_dict=True)
    context.outstanding_ap = _fmt(flt(ap[0].total) if ap else 0)

    # Active customers
    context.active_customers = frappe.db.count("Customer", {"disabled": 0}) or 0

    # Sales Orders to bill
    so_to_bill = frappe.db.sql("""
        SELECT COUNT(*) as cnt
        FROM `tabSales Order` so
        WHERE so.docstatus=1 AND so.billing_status IN ('Not Billed','Partly Billed') {cc}
    """.format(cc=cc_so), params, as_dict=True)
    context.so_to_bill = int(so_to_bill[0].cnt) if so_to_bill else 0

    # ── Monthly trend (sales vs purchase last 6 or 12 months) ──
    months = 12 if period in ("this_year", "last_year") else 6
    context.trend_labels = []
    context.trend_sales = []
    context.trend_purchase = []
    for i in range(months - 1, -1, -1):
        m_start = getdate(get_first_day(add_months(to_date, -i)))
        m_end = getdate(get_last_day(m_start))
        # Use Python strftime — frappe.utils.formatdate does NOT accept moment.js patterns
        lbl = m_start.strftime("%b %y")
        qp = {"s": m_start.strftime("%Y-%m-%d"), "e": m_end.strftime("%Y-%m-%d"), "company": company}
        s = frappe.db.sql("""
            SELECT COALESCE(SUM(si.base_grand_total),0) as t FROM `tabSales Invoice` si
            WHERE si.docstatus=1 AND si.posting_date BETWEEN %(s)s AND %(e)s {cc}
        """.format(cc=cc_si), qp, as_dict=True)
        p = frappe.db.sql("""
            SELECT COALESCE(SUM(pi.base_grand_total),0) as t FROM `tabPurchase Invoice` pi
            WHERE pi.docstatus=1 AND pi.posting_date BETWEEN %(s)s AND %(e)s {cc}
        """.format(cc=cc_pi), qp, as_dict=True)
        context.trend_labels.append(lbl)
        context.trend_sales.append(flt(s[0].t) if s else 0)
        context.trend_purchase.append(flt(p[0].t) if p else 0)

    # ═══════════════════════════════════════════
    # HR & ATTENDANCE
    # ═══════════════════════════════════════════

    context.total_employees = frappe.db.count("Employee", {"status": "Active"}) or 0
    context.job_openings = frappe.db.count("Job Opening", {"status": "Open"}) or 0

    # New hires this year
    yr_start = date(getdate(nowdate()).year, 1, 1)
    context.new_hires = frappe.db.count("Employee", {
        "date_of_joining": ["between", [yr_start, nowdate()]],
        "status": "Active"
    }) or 0

    # Separations this period
    sep = frappe.db.sql("""
        SELECT COUNT(*) as cnt FROM `tabEmployee`
        WHERE relieving_date BETWEEN %(from_date)s AND %(to_date)s
    """, params, as_dict=True)
    context.separations = int(sep[0].cnt) if sep else 0

    # Attendance breakdown (period)
    att = frappe.db.sql("""
        SELECT status, COUNT(*) as cnt
        FROM `tabAttendance`
        WHERE docstatus=1 AND attendance_date BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY status
    """, params, as_dict=True)
    att_map = {r.status: int(r.cnt) for r in att}
    context.att_present = att_map.get("Present", 0)
    context.att_absent = att_map.get("Absent", 0)
    context.att_half_day = att_map.get("Half Day", 0)
    context.att_on_leave = att_map.get("On Leave", 0)
    context.att_wfh = att_map.get("Work From Home", 0)
    context.att_holiday = att_map.get("Holiday", 0)
    context.att_total = sum(att_map.values())

    context.att_labels = ["Present", "Absent", "Half Day", "On Leave", "Work From Home", "Holiday"]
    context.att_values = [
        context.att_present, context.att_absent, context.att_half_day,
        context.att_on_leave, context.att_wfh, context.att_holiday
    ]

    # Expense claims (period)
    exp = frappe.db.sql("""
        SELECT COALESCE(SUM(total_claimed_amount),0) as total, COUNT(*) as cnt
        FROM `tabExpense Claim`
        WHERE docstatus=1 AND posting_date BETWEEN %(from_date)s AND %(to_date)s
    """, params, as_dict=True)
    context.expense_claims_amt = _fmt(flt(exp[0].total) if exp else 0)
    context.expense_claims_count = int(exp[0].cnt) if exp else 0

    # Salary (period)
    sal = frappe.db.sql("""
        SELECT COALESCE(SUM(net_pay),0) as total, COUNT(*) as cnt
        FROM `tabSalary Slip`
        WHERE docstatus=1 AND start_date >= %(from_date)s AND end_date <= %(to_date)s
    """, params, as_dict=True)
    context.salary_total = _fmt(flt(sal[0].total) if sal else 0)
    context.salary_count = int(sal[0].cnt) if sal else 0

    # Leave applications (period)
    lv = frappe.db.sql("""
        SELECT COUNT(*) as cnt FROM `tabLeave Application`
        WHERE docstatus=1 AND from_date >= %(from_date)s AND to_date <= %(to_date)s
    """, params, as_dict=True)
    context.leave_count = int(lv[0].cnt) if lv else 0

    # ═══════════════════════════════════════════
    # CRM
    # ═══════════════════════════════════════════

    # Leads by status
    leads = frappe.db.sql("""
        SELECT status, COUNT(*) as cnt
        FROM `tabLead`
        WHERE creation BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY status ORDER BY cnt DESC
    """, params, as_dict=True)
    context.leads = leads
    context.total_leads = sum(r.cnt for r in leads)

    context.lead_labels = [r.status for r in leads]
    context.lead_values = [r.cnt for r in leads]

    # Lead summary KPIs
    lead_map = {r.status: r.cnt for r in leads}
    context.leads_open = lead_map.get("Open", 0) + lead_map.get("Lead", 0)
    context.leads_converted = lead_map.get("Converted", 0)
    context.leads_interested = lead_map.get("Interested", 0)
    context.leads_lost = lead_map.get("Do Not Contact", 0) + lead_map.get("Lost Quotation", 0)

    # Quotations by status
    quotes = frappe.db.sql("""
        SELECT q.status, COUNT(*) as cnt, COALESCE(SUM(q.base_grand_total),0) as val
        FROM `tabQuotation` q
        WHERE q.docstatus IN (0,1) AND q.transaction_date BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY q.status ORDER BY cnt DESC
    """, params, as_dict=True)
    context.quotations = quotes
    context.total_quotations = sum(r.cnt for r in quotes)
    for r in context.quotations:
        r.val_fmt = _fmt(r.val)

    context.quote_labels = [r.status for r in quotes]
    context.quote_values = [r.cnt for r in quotes]

    # Opportunities
    opps = frappe.db.sql("""
        SELECT status, COUNT(*) as cnt, COALESCE(SUM(opportunity_amount),0) as val
        FROM `tabOpportunity`
        WHERE creation BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY status ORDER BY cnt DESC
    """, params, as_dict=True)
    context.opportunities = opps
    context.total_opportunities = sum(r.cnt for r in opps)
    opp_map = {r.status: r for r in opps}
    context.opp_open = opp_map.get("Open", type("", (), {"cnt": 0})()).cnt
    context.opp_pipeline_val = _fmt(sum(flt(r.val) for r in opps if r.status in ("Open", "Quotation")))

    # Recent Leads
    context.recent_leads = frappe.db.sql("""
        SELECT name, lead_name, company_name, status, lead_owner, creation
        FROM `tabLead`
        ORDER BY creation DESC LIMIT 8
    """, as_dict=True)
    for r in context.recent_leads:
        r.creation_fmt = formatdate(r.creation)

    # ═══════════════════════════════════════════
    # ASSETS
    # ═══════════════════════════════════════════

    context.total_assets = frappe.db.count("Asset", {"docstatus": 1}) or 0
    asset_val = frappe.db.sql("""
        SELECT COALESCE(SUM(gross_purchase_amount),0) as val
        FROM `tabAsset` WHERE docstatus=1
    """, as_dict=True)
    context.asset_value = _fmt(flt(asset_val[0].val) if asset_val else 0)

    new_assets = frappe.db.count("Asset", {
        "docstatus": 1,
        "purchase_date": ["between", [yr_start, nowdate()]]
    }) or 0
    context.new_assets = new_assets

    # Asset by category (top 6)
    asset_cats = frappe.db.sql("""
        SELECT asset_category, COUNT(*) as cnt, COALESCE(SUM(gross_purchase_amount),0) as val
        FROM `tabAsset` WHERE docstatus=1
        GROUP BY asset_category ORDER BY val DESC LIMIT 6
    """, as_dict=True)
    context.asset_categories = asset_cats
    context.asset_cat_labels = [r.asset_category for r in asset_cats]
    context.asset_cat_values = [flt(r.val) for r in asset_cats]

    # ═══════════════════════════════════════════
    # PROJECTS
    # ═══════════════════════════════════════════

    proj_status = frappe.db.sql("""
        SELECT status, COUNT(*) as cnt
        FROM `tabProject`
        GROUP BY status ORDER BY cnt DESC
    """, as_dict=True)
    context.project_statuses = proj_status
    context.total_projects = sum(r.cnt for r in proj_status)
    proj_map = {r.status: r.cnt for r in proj_status}
    context.proj_open = proj_map.get("Open", 0)
    context.proj_completed = proj_map.get("Completed", 0)
    context.proj_cancelled = proj_map.get("Cancelled", 0)

    # Overdue tasks
    overdue_tasks = frappe.db.sql("""
        SELECT COUNT(*) as cnt FROM `tabTask`
        WHERE status NOT IN ('Completed','Cancelled')
          AND exp_end_date < %(today)s AND exp_end_date IS NOT NULL
    """, {"today": nowdate()}, as_dict=True)
    context.overdue_tasks = int(overdue_tasks[0].cnt) if overdue_tasks else 0

    context.proj_labels = [r.status for r in proj_status]
    context.proj_values = [r.cnt for r in proj_status]

    # Recent projects
    context.recent_projects = frappe.db.sql("""
        SELECT name, project_name, status, percent_complete, expected_end_date
        FROM `tabProject`
        ORDER BY modified DESC LIMIT 6
    """, as_dict=True)
    for r in context.recent_projects:
        r.pct = int(flt(r.percent_complete))
        r.end_fmt = formatdate(r.expected_end_date) if r.expected_end_date else "—"


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
        label = f"Q{q+1} {today.year}"
        return start, end, label
    elif period == "last_quarter":
        q = (today.month - 1) // 3
        q_prev = q - 1
        yr = today.year
        if q_prev < 0:
            q_prev = 3
            yr -= 1
        start = date(yr, q_prev * 3 + 1, 1)
        end_month = start.month + 2
        end = get_last_day(date(start.year, end_month, 1))
        return start, end, f"Q{q_prev+1} {yr}"
    elif period == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), str(today.year - 1)
    else:  # this_year default
        return date(today.year, 1, 1), date(today.year, 12, 31), str(today.year)


def _fmt(amount):
    return "{:,.0f}".format(flt(amount))
