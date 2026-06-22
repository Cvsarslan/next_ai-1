"""Automated due-date reminders for invoices.

Runs daily from the scheduler. For every submitted, unpaid invoice that has a due
date, it can email the party (customer / supplier) and raise an in-system
notification at three configurable milestones relative to the due date:

    * upcoming  — N days before the due date   (reminder_days_before)
    * due       — on the due date              (reminder_on_due_date)
    * overdue   — N days after the due date     (reminder_days_overdue)

All behaviour is controlled from NextAI Settings:
    enable_due_date_reminders, remind_sales_invoices, remind_purchase_invoices,
    reminder_days_before, reminder_on_due_date, reminder_days_overdue,
    reminder_notify_email, reminder_notify_system.

Each milestone is sent at most once per invoice; a marker Comment is written on
the invoice so re-runs never double-send.
"""

import frappe
from frappe.utils import getdate, date_diff, today, fmt_money, get_url_to_form, escape_html, formatdate

_MARKER = "[next_ai-due-reminder:{milestone}]"

# Per-doctype configuration (party resolution + paid statuses to skip).
DOCTYPE_CONFIG = {
    "Sales Invoice": {
        "party_field": "customer",
        "party_name_field": "customer_name",
        "party_doctype": "Customer",
        "party_label": "customer",
        "skip_status": ["Paid", "Credit Note Issued", "Return"],
        "setting": "remind_sales_invoices",
    },
    "Purchase Invoice": {
        "party_field": "supplier",
        "party_name_field": "supplier_name",
        "party_doctype": "Supplier",
        "party_label": "supplier",
        "skip_status": ["Paid", "Return", "Debit Note Issued"],
        "setting": "remind_purchase_invoices",
    },
}


def _settings():
    """Read reminder settings from NextAI Settings with sensible defaults."""
    g = frappe.db.get_singles_dict("NextAI Settings") or {}

    def b(key, default):
        v = g.get(key)
        return default if v is None or v == "" else bool(int(v))

    def i(key, default):
        try:
            return int(g.get(key))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": b("enable_due_date_reminders", True),
        "sales": b("remind_sales_invoices", True),
        "purchase": b("remind_purchase_invoices", False),
        "days_before": i("reminder_days_before", 3),
        "on_due": b("reminder_on_due_date", True),
        "days_overdue": i("reminder_days_overdue", 7),
        "notify_email": b("reminder_notify_email", True),
        "notify_system": b("reminder_notify_system", True),
    }


def _milestones(s):
    """Build {milestone: offset-from-due-date} from settings (negative = before)."""
    m = {}
    if s["days_before"] and s["days_before"] > 0:
        m["upcoming"] = -s["days_before"]
    if s["on_due"]:
        m["due"] = 0
    if s["days_overdue"] and s["days_overdue"] > 0:
        m["overdue"] = s["days_overdue"]
    return m


def send_due_date_reminders():
    """Scheduler entry point (daily). Safe to re-run — milestones de-duplicate."""
    s = _settings()
    if not s["enabled"] or not (s["notify_email"] or s["notify_system"]):
        return
    milestones = _milestones(s)
    if not milestones:
        return

    t = getdate(today())
    for doctype, cfg in DOCTYPE_CONFIG.items():
        if not s.get("sales" if doctype == "Sales Invoice" else "purchase"):
            continue
        _process_doctype(doctype, cfg, milestones, t, s)
    frappe.db.commit()


def _process_doctype(doctype, cfg, milestones, t, s):
    invoices = frappe.get_all(
        doctype,
        filters={"docstatus": 1, "status": ["not in", cfg["skip_status"]],
                 "outstanding_amount": [">", 0], "due_date": ["is", "set"]},
        fields=["name", cfg["party_field"], cfg["party_name_field"], "due_date",
                "outstanding_amount", "currency", "company", "owner", "contact_email"],
    )
    for inv in invoices:
        try:
            milestone = _milestone_for(getdate(inv.due_date), t, milestones)
            if not milestone or _already_sent(doctype, inv.name, milestone):
                continue
            _send_one(doctype, cfg, inv, milestone, s)
            _mark_sent(doctype, inv.name, milestone)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Due-date reminder: {inv.name}")


def _milestone_for(due_date, t, milestones):
    """Return the milestone whose target date (due_date + offset) is today."""
    diff = date_diff(due_date, t)   # days from today until due (negative = overdue)
    for milestone, offset in milestones.items():
        if diff == -offset:
            return milestone
    return None


def _already_sent(doctype, invoice, milestone):
    return bool(frappe.db.exists("Comment", {
        "reference_doctype": doctype,
        "reference_name": invoice,
        "comment_type": "Comment",
        "content": _MARKER.format(milestone=milestone),
    }))


def _mark_sent(doctype, invoice, milestone):
    frappe.get_doc({
        "doctype": "Comment",
        "comment_type": "Comment",
        "reference_doctype": doctype,
        "reference_name": invoice,
        "content": _MARKER.format(milestone=milestone),
    }).insert(ignore_permissions=True)


def _party_email(cfg, inv):
    """Resolve the best email for the party: invoice contact, party record, then
    the party's primary linked contact."""
    if inv.get("contact_email"):
        return inv["contact_email"]
    party = inv.get(cfg["party_field"])
    if not party:
        return None
    if frappe.get_meta(cfg["party_doctype"]).has_field("email_id"):
        email = frappe.db.get_value(cfg["party_doctype"], party, "email_id")
        if email:
            return email
    rows = frappe.db.sql(
        """SELECT c.email_id
           FROM `tabContact` c
           JOIN `tabDynamic Link` dl ON dl.parent = c.name AND dl.parenttype = 'Contact'
           WHERE dl.link_doctype = %s AND dl.link_name = %s
             AND IFNULL(c.email_id, '') != ''
           ORDER BY c.is_primary_contact DESC, c.modified DESC LIMIT 1""",
        (cfg["party_doctype"], party),
    )
    return rows[0][0] if rows else None


def _wording(doctype, name, due, milestone):
    """Return (email_subject, email_lead, system_subject) per doctype & milestone."""
    is_sales = doctype == "Sales Invoice"
    noun = "Invoice" if is_sales else "Bill"
    if milestone == "upcoming":
        return (
            f"Reminder: {noun} {name} due on {due}",
            f"This is a friendly reminder that the {noun.lower()} below is due on <b>{due}</b>.",
            f"{noun} {name} due soon ({due})",
        )
    if milestone == "due":
        return (
            f"{noun} {name} is due today",
            f"The {noun.lower()} below is due <b>today ({due})</b>.",
            f"{noun} {name} is due today",
        )
    return (
        f"Overdue: {noun} {name} ({due})",
        f"The {noun.lower()} below was due on <b>{due}</b> and is now <b>overdue</b>. "
        "Please arrange payment at your earliest convenience.",
        f"{noun} {name} is overdue (due {due})",
    )


def _send_one(doctype, cfg, inv, milestone, s):
    cur = inv.get("currency") or frappe.get_value("Company", inv["company"], "default_currency") or "AED"
    amount = fmt_money(inv["outstanding_amount"], currency=cur)
    due = formatdate(inv["due_date"])
    link = get_url_to_form(doctype, inv["name"])
    party_name = inv.get(cfg["party_name_field"]) or inv.get(cfg["party_field"])
    subject, lead, sys_subject = _wording(doctype, inv["name"], due, milestone)
    noun = "Invoice" if doctype == "Sales Invoice" else "Bill"

    # 1) Email the party (non-fatal: notifications still fire if email fails).
    if s["notify_email"]:
        email = _party_email(cfg, inv)
        if email:
            message = f"""
                <p>Dear {escape_html(party_name)},</p>
                <p>{lead}</p>
                <table style="border-collapse:collapse;margin:12px 0">
                  <tr><td style="padding:4px 12px 4px 0;color:#666">{noun}</td><td style="padding:4px 0"><b>{inv['name']}</b></td></tr>
                  <tr><td style="padding:4px 12px 4px 0;color:#666">Due date</td><td style="padding:4px 0">{due}</td></tr>
                  <tr><td style="padding:4px 12px 4px 0;color:#666">Amount due</td><td style="padding:4px 0"><b>{amount}</b></td></tr>
                </table>
                <p>If the payment has already been made, please ignore this message. Thank you.</p>
            """
            try:
                frappe.sendmail(recipients=[email], subject=subject, message=message,
                                reference_doctype=doctype, reference_name=inv["name"])
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"Due-date reminder email: {inv['name']}")

    # 2) Raise an in-system notification for the relevant desk users.
    if s["notify_system"]:
        for user in _notify_users(inv):
            try:
                frappe.get_doc({
                    "doctype": "Notification Log",
                    "subject": sys_subject,
                    "email_content": f"{amount} outstanding on {inv['name']} ({cfg['party_label']} {party_name}). {link}",
                    "for_user": user,
                    "type": "Alert",
                    "document_type": doctype,
                    "document_name": inv["name"],
                    "from_user": inv.get("owner"),
                }).insert(ignore_permissions=True)
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"Due-date notification: {inv['name']}")


def _notify_users(inv):
    """Desk users to notify in-system: the invoice owner plus Accounts Managers."""
    users = set()
    owner = inv.get("owner")
    if owner and owner not in ("Administrator", "Guest") and frappe.db.get_value("User", owner, "enabled"):
        users.add(owner)
    managers = frappe.get_all(
        "Has Role", filters={"role": "Accounts Manager", "parenttype": "User"}, pluck="parent")
    for u in managers:
        if u not in ("Administrator", "Guest") and frappe.db.get_value("User", u, "enabled"):
            users.add(u)
    return users


@frappe.whitelist()
def run_due_date_reminders_now():
    """Manually trigger the reminder run (System Manager only) for testing."""
    frappe.only_for("System Manager")
    send_due_date_reminders()
    return {"ok": True, "message": "Due-date reminders processed."}
