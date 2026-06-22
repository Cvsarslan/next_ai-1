"""CRM outreach for the client portal.

Bulk-email tooling that lets portal users reach an audience in one click:

  * all customers (with an email on file)
  * customers with an outstanding balance
  * all CRM leads (optionally filtered by status)
  * all contacts

Plus helpers to preview the recipient list before sending and to send a test
email to a single address first.

Sends are queued through frappe.sendmail (each recipient gets their own email so
addresses are never exposed to one another) and are capped to avoid runaways.
"""

import frappe
from frappe.utils import validate_email_address, escape_html

# Hard cap on a single send to protect the mail queue / reputation.
MAX_RECIPIENTS = 2000


def _guard():
    if frappe.session.user in ("Guest", None, ""):
        frappe.throw("You must be signed in to use CRM outreach.")


# ── Recipient resolution ─────────────────────────────────────────────────────
def _customer_recipients(company=None, with_outstanding=False, customer_group=None, territory=None):
    conds = ["c.disabled = 0"]
    params = {}
    if customer_group:
        conds.append("c.customer_group = %(cg)s"); params["cg"] = customer_group
    if territory:
        conds.append("c.territory = %(terr)s"); params["terr"] = territory
    where = " AND ".join(conds)

    outstanding_join = ""
    if with_outstanding:
        outstanding_join = """
            JOIN (SELECT customer FROM `tabSales Invoice`
                  WHERE docstatus=1 AND outstanding_amount>0
                  {co} GROUP BY customer) si ON si.customer = c.name
        """.format(co=("AND company=%(co)s" if company else ""))
        if company:
            params["co"] = company

    rows = frappe.db.sql("""
        SELECT c.name, c.customer_name AS label,
               COALESCE(NULLIF(c.email_id, ''), ct.email_id) AS email
        FROM `tabCustomer` c
        {oj}
        LEFT JOIN `tabDynamic Link` dl
               ON dl.link_doctype='Customer' AND dl.link_name=c.name AND dl.parenttype='Contact'
        LEFT JOIN `tabContact` ct ON ct.name = dl.parent AND IFNULL(ct.email_id,'')!=''
        WHERE {where}
        GROUP BY c.name
    """.format(oj=outstanding_join, where=where), params, as_dict=True)
    return rows


def _lead_recipients(lead_status=None):
    conds = ["IFNULL(email_id,'') != ''", "status != 'Do Not Contact'"]
    params = {}
    if lead_status and lead_status != "All":
        conds.append("status = %(st)s"); params["st"] = lead_status
    return frappe.db.sql("""
        SELECT name, lead_name AS label, email_id AS email
        FROM `tabLead` WHERE {w}
    """.format(w=" AND ".join(conds)), params, as_dict=True)


def _contact_recipients():
    return frappe.db.sql("""
        SELECT name,
               TRIM(CONCAT(IFNULL(first_name,''),' ',IFNULL(last_name,''))) AS label,
               email_id AS email
        FROM `tabContact` WHERE IFNULL(email_id,'') != ''
    """, as_dict=True)


def _resolve(audience, company=None, customer_group=None, territory=None, lead_status=None):
    if audience == "customers":
        rows = _customer_recipients(company, False, customer_group, territory)
    elif audience == "customers_outstanding":
        rows = _customer_recipients(company, True, customer_group, territory)
    elif audience == "leads":
        rows = _lead_recipients(lead_status)
    elif audience == "contacts":
        rows = _contact_recipients()
    else:
        frappe.throw(f"Unknown audience: {audience}")

    seen, clean = set(), []
    no_email = 0
    for r in rows:
        email = (r.get("email") or "").strip().lower()
        if not email:
            no_email += 1
            continue
        if email in seen:
            continue
        try:
            validate_email_address(email, throw=True)
        except Exception:
            continue
        seen.add(email)
        clean.append({"name": r.get("name"), "label": r.get("label") or r.get("name"), "email": email})
    return clean, no_email


# ── API ──────────────────────────────────────────────────────────────────────
@frappe.whitelist()
def get_outreach_audiences(company=None):
    """Counts for the outreach audience cards."""
    _guard()
    if not company:
        from next_ai.api import _get_company
        company = _get_company()

    def count(aud):
        try:
            recips, _ = _resolve(aud, company=company)
            return len(recips)
        except Exception:
            return 0

    return {
        "customers": count("customers"),
        "customers_outstanding": count("customers_outstanding"),
        "leads": count("leads"),
        "contacts": count("contacts"),
        "customer_groups": frappe.get_all("Customer Group", filters={"is_group": 0}, pluck="name"),
        "territories": frappe.get_all("Territory", filters={"is_group": 0}, pluck="name"),
        "lead_statuses": ["All", "Open", "Replied", "Opportunity", "Interested", "Quotation", "Converted"],
    }


@frappe.whitelist()
def preview_recipients(audience, company=None, customer_group=None, territory=None, lead_status=None):
    """Return recipient count + a small sample for confirmation before sending."""
    _guard()
    if not company:
        from next_ai.api import _get_company
        company = _get_company()
    recips, no_email = _resolve(audience, company, customer_group, territory, lead_status)
    return {
        "count": len(recips),
        "no_email_count": no_email,
        "sample": recips[:8],
        "capped": len(recips) > MAX_RECIPIENTS,
        "max": MAX_RECIPIENTS,
    }


@frappe.whitelist()
def send_outreach_email(audience, subject, message, company=None, customer_group=None,
                        territory=None, lead_status=None, test_email=None):
    """Send a bulk email to the resolved audience, or a single test email.

    Each recipient receives their own email (no shared To/CC), with an optional
    {name} placeholder in the message replaced by the recipient's name.
    """
    _guard()
    subject = (subject or "").strip()
    message = (message or "").strip()
    if not subject or not message:
        frappe.throw("Subject and message are both required.")

    # Test send: deliver one email to the given address and stop.
    if test_email:
        validate_email_address(test_email, throw=True)
        frappe.sendmail(recipients=[test_email], subject=f"[TEST] {subject}",
                        message=_render(message, "there"), now=False)
        return {"ok": True, "test": True, "sent": 1}

    if not company:
        from next_ai.api import _get_company
        company = _get_company()
    recips, no_email = _resolve(audience, company, customer_group, territory, lead_status)
    if not recips:
        frappe.throw("No recipients with a valid email were found for this audience.")

    capped = False
    if len(recips) > MAX_RECIPIENTS:
        recips = recips[:MAX_RECIPIENTS]
        capped = True

    sent = 0
    for r in recips:
        try:
            frappe.sendmail(recipients=[r["email"]], subject=subject,
                            message=_render(message, r["label"]), now=False)
            sent += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"CRM outreach to {r['email']}")

    # Audit trail visible to the sender.
    try:
        frappe.get_doc({
            "doctype": "Notification Log",
            "subject": f"CRM email sent to {sent} recipient(s)",
            "email_content": f"Audience: {audience} · Subject: {subject}",
            "for_user": frappe.session.user,
            "type": "Alert",
        }).insert(ignore_permissions=True)
    except Exception:
        pass

    return {"ok": True, "sent": sent, "no_email_count": no_email, "capped": capped, "max": MAX_RECIPIENTS}


def _render(message, name):
    """Substitute {name} and prepend a greeting if the author didn't add one."""
    body = message.replace("{name}", escape_html(name or "there"))
    if "{name}" not in message and "<p>dear" not in message.lower() and "hi " not in message[:10].lower():
        body = f"<p>Dear {escape_html(name or 'there')},</p>{body}"
    return body
