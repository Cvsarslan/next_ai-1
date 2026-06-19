# Sample HR / Payroll data generator for NextAI (UAE).
#
# Idempotent: safe to run repeatedly. Creates Salary Components, a Salary
# Structure, Structure Assignments, Salary Slips, a Payroll Entry, Loan
# Applications, Leave Allocations + Applications, and Attendance records for
# the existing active employees.
#
# Run:  bench --site <site> execute next_ai.sample_hr_data.create_sample_hr_data

import frappe
from frappe.utils import flt, add_days, getdate, get_first_day, get_last_day, add_months, nowdate

STRUCTURE = "Standard UAE Structure"

# (component, abbr, type, formula-on-base)
COMPONENTS = [
    ("Basic", "B", "Earning", "base * 0.6"),
    ("Housing Allowance", "HA", "Earning", "base * 0.25"),
    ("Transport Allowance", "TA", "Earning", "base * 0.15"),
    ("Social Insurance", "SI", "Deduction", "base * 0.05"),
]

# Base monthly salary per department (AED)
DEPT_BASE = {
    "Sales": 12000, "Marketing": 11000, "Operations": 13000,
    "Accounts": 14000, "Human Resources": 12500, "default": 10000,
}

LOAN_SAMPLES = [
    ("Personal Loan", 20000, 12, "Home renovation", "Approved"),
    ("Salary Advance", 5000, 3, "Personal emergency", "Pending Approval"),
    ("Vehicle Loan", 45000, 24, "Car purchase", "Approved"),
    ("Emergency Loan", 8000, 6, "Medical expenses", "Rejected"),
]


def _company():
    return frappe.db.get_single_value("Global Defaults", "default_company") \
        or frappe.get_all("Company", pluck="name", limit=1)[0]


def _currency(company):
    return frappe.db.get_value("Company", company, "default_currency") or "AED"


def _dept_base(department):
    if not department:
        return DEPT_BASE["default"]
    key = department.split(" - ")[0].strip()
    return DEPT_BASE.get(key, DEPT_BASE["default"])


def _ensure_holiday_list(company):
    """Create a Holiday List with UAE weekends (Sat/Sun) and set it as the
    company default + on any employee missing one. Required for salary slips
    and leave processing."""
    year = getdate(nowdate()).year
    name = "UAE Weekends %d" % year
    if not frappe.db.exists("Holiday List", name):
        yr_start, yr_end = getdate("%d-01-01" % year), getdate("%d-12-31" % year)
        holidays, d = [], yr_start
        while d <= yr_end:
            if d.weekday() in (5, 6):  # Sat, Sun
                holidays.append({"holiday_date": d, "description": "Weekend", "weekly_off": 1})
            d = add_days(d, 1)
        frappe.get_doc({
            "doctype": "Holiday List",
            "holiday_list_name": name,
            "from_date": yr_start,
            "to_date": yr_end,
            "holidays": holidays,
        }).insert(ignore_permissions=True)

    if not frappe.db.get_value("Company", company, "default_holiday_list"):
        frappe.db.set_value("Company", company, "default_holiday_list", name)

    for emp in frappe.get_all("Employee", filters={"status": "Active", "company": company,
                                                   "holiday_list": ("in", ["", None])}, pluck="name"):
        frappe.db.set_value("Employee", emp, "holiday_list", name)
    frappe.db.commit()
    return name


UAE_BANKS = ["Emirates NBD", "Abu Dhabi Commercial Bank", "First Abu Dhabi Bank",
             "Mashreq Bank", "RAKBANK", "Dubai Islamic Bank"]


def _set_employee_bank(employees):
    """Populate sample UAE bank details on Employee master (bank_name / bank_ac_no / iban)."""
    count = 0
    for i, e in enumerate(employees):
        if frappe.db.get_value("Employee", e.name, "bank_ac_no"):
            continue
        bank = UAE_BANKS[i % len(UAE_BANKS)]
        acc = "01%09d" % (1234567 + i * 37)
        iban = "AE%02d0%s%012d" % ((i * 7) % 100, "260", 1234567890 + i * 911)
        try:
            frappe.db.set_value("Employee", e.name, {
                "bank_name": bank,
                "bank_ac_no": acc,
                "iban": iban[:23],
                "salary_mode": "Bank",
            })
            count += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), "sample_hr: bank %s" % e.name)
    frappe.db.commit()
    return count


def _ensure_components(company):
    for name, abbr, ctype, _ in COMPONENTS:
        if frappe.db.exists("Salary Component", name):
            continue
        frappe.get_doc({
            "doctype": "Salary Component",
            "salary_component": name,
            "salary_component_abbr": abbr,
            "type": ctype,
            "depends_on_payment_days": 1 if name in ("Basic",) else 0,
            "is_tax_applicable": 0,
        }).insert(ignore_permissions=True)


def _ensure_structure(company):
    currency = _currency(company)
    if frappe.db.exists("Salary Structure", STRUCTURE):
        return STRUCTURE
    doc = frappe.get_doc({
        "doctype": "Salary Structure",
        "name": STRUCTURE,
        "company": company,
        "currency": currency,
        "payroll_frequency": "Monthly",
        "is_active": "Yes",
        "earnings": [
            {"salary_component": n, "abbr": a, "amount_based_on_formula": 1, "formula": f}
            for n, a, t, f in COMPONENTS if t == "Earning"
        ],
        "deductions": [
            {"salary_component": n, "abbr": a, "amount_based_on_formula": 1, "formula": f}
            for n, a, t, f in COMPONENTS if t == "Deduction"
        ],
    })
    doc.insert(ignore_permissions=True)
    doc.submit()
    return STRUCTURE


def _assign_structures(company, employees):
    currency = _currency(company)
    count = 0
    for e in employees:
        exists = frappe.db.exists("Salary Structure Assignment",
                                  {"employee": e.name, "salary_structure": STRUCTURE, "docstatus": 1})
        if exists:
            continue
        from_date = max(getdate(e.date_of_joining or "2024-01-01"), getdate("2024-01-01"))
        try:
            ssa = frappe.get_doc({
                "doctype": "Salary Structure Assignment",
                "employee": e.name,
                "salary_structure": STRUCTURE,
                "from_date": from_date,
                "company": company,
                "currency": currency,
                "base": _dept_base(e.department),
            })
            ssa.insert(ignore_permissions=True)
            ssa.submit()
            count += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), "sample_hr: assign %s" % e.name)
    return count


def _make_salary_slips(company, employees, months=2):
    """Create draft salary slips for the last `months` complete months."""
    made = 0
    today = getdate(nowdate())
    for m in range(1, months + 1):
        ref = add_months(today, -m)
        start, end = get_first_day(ref), get_last_day(ref)
        for e in employees:
            if frappe.db.exists("Salary Slip",
                                {"employee": e.name, "start_date": start, "end_date": end}):
                continue
            try:
                slip = frappe.get_doc({
                    "doctype": "Salary Slip",
                    "employee": e.name,
                    "company": company,
                    "posting_date": end,
                    "salary_structure": STRUCTURE,
                    "payroll_frequency": "Monthly",
                    "start_date": start,
                    "end_date": end,
                })
                slip.insert(ignore_permissions=True)
                made += 1
            except Exception:
                frappe.log_error(frappe.get_traceback(), "sample_hr: slip %s %s" % (e.name, start))
    return made


def _make_payroll_entry(company, months=1):
    """Attempt a Payroll Entry for last month. Skipped gracefully if accounts
    are not configured (UAE setups often post payroll manually)."""
    try:
        ref = add_months(getdate(nowdate()), -months)
        start, end = get_first_day(ref), get_last_day(ref)
        if frappe.db.exists("Payroll Entry",
                            {"start_date": start, "end_date": end, "company": company, "docstatus": ("<", 2)}):
            return "exists"
        pe = frappe.get_doc({
            "doctype": "Payroll Entry",
            "company": company,
            "posting_date": end,
            "payroll_frequency": "Monthly",
            "start_date": start,
            "end_date": end,
            "currency": _currency(company),
        })
        pe.insert(ignore_permissions=True)
        try:
            pe.fill_employee_details()
            pe.save(ignore_permissions=True)
        except Exception:
            pass
        return pe.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), "sample_hr: payroll entry")
        return None


def _make_loans(company, employees):
    made = 0
    for i, e in enumerate(employees[:len(LOAN_SAMPLES)]):
        loan_type, amount, months, purpose, status = LOAN_SAMPLES[i % len(LOAN_SAMPLES)]
        if frappe.db.exists("Employee Loan Application", {"employee": e.name, "loan_type": loan_type}):
            continue
        try:
            monthly = round(flt(amount) / max(1, months), 2)
            doc = frappe.get_doc({
                "doctype": "Employee Loan Application",
                "employee": e.name,
                "employee_name": e.employee_name,
                "company": company,
                "requested_on": add_days(nowdate(), -(i + 1) * 7),
                "loan_type": loan_type,
                "requested_amount": amount,
                "repayment_months": months,
                "monthly_repayment": monthly,
                "purpose": purpose,
                "status": status,
                "decision_notes": "Auto-approved sample" if status == "Approved" else
                                  ("Insufficient tenure" if status == "Rejected" else None),
            })
            doc.insert(ignore_permissions=True)
            made += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), "sample_hr: loan %s" % e.name)
    return made


def _make_leaves(company, employees):
    year = getdate(nowdate()).year
    yr_start, yr_end = getdate("%d-01-01" % year), getdate("%d-12-31" % year)
    leave_type = "Annual Leave" if frappe.db.exists("Leave Type", "Annual Leave") \
        else frappe.get_all("Leave Type", pluck="name", limit=1)[0]
    alloc, apps = 0, 0
    for idx, e in enumerate(employees[:6]):
        # Allocation
        if not frappe.db.exists("Leave Allocation",
                                {"employee": e.name, "leave_type": leave_type, "docstatus": 1,
                                 "from_date": (">=", yr_start)}):
            try:
                la = frappe.get_doc({
                    "doctype": "Leave Allocation",
                    "employee": e.name,
                    "leave_type": leave_type,
                    "from_date": yr_start,
                    "to_date": yr_end,
                    "new_leaves_allocated": 30,
                    "company": company,
                })
                la.insert(ignore_permissions=True)
                la.submit()
                alloc += 1
            except Exception:
                frappe.log_error(frappe.get_traceback(), "sample_hr: alloc %s" % e.name)
        # Application
        f = add_days(nowdate(), -(idx + 1) * 6)
        t = add_days(f, 1)
        if frappe.db.exists("Leave Application", {"employee": e.name, "from_date": f}):
            continue
        try:
            app = frappe.get_doc({
                "doctype": "Leave Application",
                "employee": e.name,
                "leave_type": leave_type,
                "from_date": f,
                "to_date": t,
                "company": company,
                "status": "Approved",
                "description": "Sample leave request",
            })
            app.insert(ignore_permissions=True)
            try:
                app.submit()
            except Exception:
                pass
            apps += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), "sample_hr: leave %s" % e.name)
    return alloc, apps


def _make_attendance(company, employees, days=10):
    made = 0
    statuses = ["Present", "Present", "Present", "Present", "Half Day", "Present", "Absent"]
    for e in employees:
        for d in range(1, days + 1):
            adate = add_days(nowdate(), -d)
            if getdate(adate).weekday() >= 5:  # skip Sat/Sun
                continue
            if frappe.db.exists("Attendance", {"employee": e.name, "attendance_date": adate, "docstatus": ("<", 2)}):
                continue
            try:
                att = frappe.get_doc({
                    "doctype": "Attendance",
                    "employee": e.name,
                    "attendance_date": adate,
                    "status": statuses[(d + hash(e.name)) % len(statuses)],
                    "company": company,
                })
                att.insert(ignore_permissions=True)
                att.submit()
                made += 1
            except Exception:
                frappe.log_error(frappe.get_traceback(), "sample_hr: attendance %s %s" % (e.name, adate))
    return made


def create_sample_hr_data():
    company = _company()
    employees = frappe.get_all(
        "Employee", filters={"status": "Active", "company": company},
        fields=["name", "employee_name", "department", "date_of_joining"],
        order_by="name asc", limit=15,
    )
    if not employees:
        return {"error": "No active employees found for company %s" % company}

    summary = {"company": company, "employees": len(employees)}
    summary["holiday_list"] = _ensure_holiday_list(company)
    summary["employee_bank_details"] = _set_employee_bank(employees)
    _ensure_components(company)
    summary["structure"] = _ensure_structure(company)
    summary["assignments"] = _assign_structures(company, employees)
    summary["salary_slips"] = _make_salary_slips(company, employees, months=2)
    summary["payroll_entry"] = _make_payroll_entry(company, months=1)
    summary["loan_applications"] = _make_loans(company, employees)
    alloc, apps = _make_leaves(company, employees)
    summary["leave_allocations"], summary["leave_applications"] = alloc, apps
    summary["attendance_records"] = _make_attendance(company, employees, days=10)
    frappe.db.commit()
    return summary
