# Sample bank accounts, bank transactions and notifications for NextAI.
# Idempotent. Run:
#   bench --site <site> execute next_ai.sample_bank_data.create_sample_bank_data

import frappe
from frappe.utils import flt, add_days, nowdate, getdate

from next_ai.api import create_bank_account, _get_company

SAMPLE_ACCOUNTS = [
    {"account_name": "Emirates NBD Current", "bank": "Emirates NBD",
     "account_no": "1015432198001", "iban": "", "default": True},
    {"account_name": "Mashreq Operations", "bank": "Mashreq Bank",
     "account_no": "0194556677001", "iban": "", "default": False},
]

# (days_ago, description, deposit, withdrawal)
SAMPLE_TXNS = [
    (2, "Customer payment received", 18500, 0),
    (4, "Supplier transfer - Global Office", 0, 4800),
    (6, "Customer payment - TechPro", 32000, 0),
    (8, "Salary disbursement", 0, 56000),
    (11, "Card settlement", 7250, 0),
    (14, "Utility bill - DEWA", 0, 1320),
    (18, "Customer payment received", 12750, 0),
    (22, "Rent payment", 0, 15000),
    (26, "Consulting income", 28000, 0),
    (30, "Bank charges", 0, 185),
]


def _ensure_accounts(company):
    made, names = 0, []
    for a in SAMPLE_ACCOUNTS:
        existing = frappe.db.exists("Bank Account", {"account_name": a["account_name"], "company": company})
        if existing:
            names.append(existing)
            continue
        try:
            res = create_bank_account(account_name=a["account_name"], bank=a["bank"],
                                      account_no=a["account_no"], iban=a["iban"], company=company)
            names.append(res["name"])
            if a.get("default"):
                frappe.db.set_value("Bank Account", res["name"], "is_default", 1)
            made += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), "sample_bank: account %s" % a["account_name"])
    frappe.db.commit()
    return made, names


OPENING_BALANCES = {"Emirates NBD Current": 250000, "Mashreq Operations": 85000}


def _post_opening_balances(company, account_names):
    """Give the bank GL accounts real balances via an Opening Entry Journal so they
    appear under Bank & Cash Balances (which hides zero-balance accounts)."""
    # Find a balancing account: Temporary Opening, else any non-group Equity account.
    balancing = frappe.db.get_value("Account", {"company": company, "account_type": "Temporary", "is_group": 0}, "name") \
        or frappe.db.get_value("Account", {"company": company, "root_type": "Equity", "is_group": 0}, "name")
    if not balancing:
        return 0
    rows, total = [], 0
    for ba in account_names:
        gl = frappe.db.get_value("Bank Account", ba, "account")
        acct_name = frappe.db.get_value("Bank Account", ba, "account_name")
        amt = OPENING_BALANCES.get(acct_name)
        if not gl or not amt:
            continue
        # Skip if this bank GL account already has entries.
        if frappe.db.exists("GL Entry", {"account": gl, "company": company, "is_cancelled": 0}):
            continue
        rows.append({"account": gl, "debit_in_account_currency": amt, "debit": amt})
        total += amt
    if not rows:
        return 0
    rows.append({"account": balancing, "credit_in_account_currency": total, "credit": total})
    try:
        je = frappe.get_doc({
            "doctype": "Journal Entry",
            "voucher_type": "Opening Entry",
            "company": company,
            "posting_date": add_days(nowdate(), -30),
            "is_opening": "Yes",
            "user_remark": "Sample opening bank balances",
            "accounts": rows,
        })
        je.insert(ignore_permissions=True)
        je.submit()
        frappe.db.commit()
        return total
    except Exception:
        frappe.log_error(frappe.get_traceback(), "sample_bank: opening balance")
        return 0


def _ensure_transactions(company, bank_account):
    currency = frappe.db.get_value("Company", company, "default_currency") or "AED"
    made = 0
    for days, desc, deposit, withdrawal in SAMPLE_TXNS:
        date = add_days(nowdate(), -days)
        ref = "SMPL-%s-%s" % (bank_account[:6], days)
        if frappe.db.exists("Bank Transaction", {"bank_account": bank_account, "reference_number": ref}):
            continue
        try:
            bt = frappe.get_doc({
                "doctype": "Bank Transaction",
                "date": date,
                "bank_account": bank_account,
                "company": company,
                "currency": currency,
                "deposit": flt(deposit),
                "withdrawal": flt(withdrawal),
                "description": desc,
                "reference_number": ref,
            })
            bt.insert(ignore_permissions=True)
            bt.submit()
            made += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), "sample_bank: txn %s" % ref)
    frappe.db.commit()
    return made


def _first(doctype, filters=None, order_by="creation desc"):
    rows = frappe.get_all(doctype, filters=filters or {}, pluck="name", order_by=order_by, limit=1)
    return rows[0] if rows else None


def _build_sample_notifs():
    """Build notifications pointing at REAL documents so each one opens its record."""
    notifs = []
    si = _first("Sales Invoice", {"docstatus": 1})
    if si:
        notifs.append(("Invoice %s is overdue" % si, "Alert", "Sales Invoice", si))
    pi = _first("Purchase Invoice")
    if pi:
        sup = frappe.db.get_value("Purchase Invoice", pi, "supplier_name") or "a supplier"
        notifs.append(("New purchase bill received from %s" % sup, "Energy Point", "Purchase Invoice", pi))
    ss = _first("Salary Slip")
    if ss:
        notifs.append(("Salary slips for last month are ready to review", "Energy Point", "Salary Slip", ss))
    la = _first("Leave Application")
    if la:
        emp = frappe.db.get_value("Leave Application", la, "employee_name") or "an employee"
        notifs.append(("Leave request from %s awaiting approval" % emp, "Alert", "Leave Application", la))
    bt = _first("Bank Transaction")
    if bt:
        notifs.append(("Bank transaction of AED 18,500 received", "Energy Point", "Bank Transaction", bt))
    tk = _first("Task")
    if tk:
        subj = frappe.db.get_value("Task", tk, "subject") or tk
        notifs.append(("New task assigned: %s" % subj, "Assignment", "Task", tk))
    # No document — a general reminder (non-clickable)
    notifs.append(("VAT return filing is due in 10 days", "Alert", None, None))
    return notifs


def _ensure_notifications(user=None):
    user = user or frappe.session.user
    if user == "Guest":
        user = "Administrator"
    # Refresh: clear our previous sample notifications so they get the document links.
    old = frappe.get_all("Notification Log", filters={"for_user": user}, pluck="name")
    for n in old:
        try:
            frappe.delete_doc("Notification Log", n, force=1, ignore_permissions=True)
        except Exception:
            pass
    made = 0
    for i, (subject, ntype, dt, dn) in enumerate(_build_sample_notifs()):
        try:
            doc = frappe.get_doc({
                "doctype": "Notification Log",
                "for_user": user,
                "subject": subject,
                "type": ntype,
                "document_type": dt,
                "document_name": dn,
                "email_content": subject,
                "read": 1 if i >= 4 else 0,
            })
            doc.insert(ignore_permissions=True)
            made += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), "sample_bank: notif")
    frappe.db.commit()
    return made


def create_sample_bank_data(user=None):
    company = _get_company()
    summary = {"company": company}
    made, accounts = _ensure_accounts(company)
    summary["bank_accounts_created"] = made
    summary["bank_accounts"] = accounts
    summary["opening_balance_posted"] = _post_opening_balances(company, accounts)
    txns = 0
    if accounts:
        txns = _ensure_transactions(company, accounts[0])
        if len(accounts) > 1:
            txns += _ensure_transactions(company, accounts[1])
    summary["bank_transactions"] = txns
    summary["notifications"] = _ensure_notifications(user)
    return summary
