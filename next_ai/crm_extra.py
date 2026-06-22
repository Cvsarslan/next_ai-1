"""Extra CRM features for the client portal.

  * Contacts        — list / create / update contacts.
  * Follow-ups      — schedule follow-up ToDos and log activities; timeline.
  * Campaigns       — list / create marketing campaigns.
  * Lead import     — bulk-create leads from rows (with campaign/source).
  * WhatsApp outreach — bulk WhatsApp messages by audience (uses frappe_whatsapp).
  * Sales Team      — sales persons with attributed sales.
"""

import frappe
from frappe.utils import cstr, flt, today, getdate, validate_phone_number


def _guard():
    if frappe.session.user in ("Guest", None, ""):
        frappe.throw("You must be signed in.")


# ── Contacts ─────────────────────────────────────────────────────────────────
@frappe.whitelist()
def list_contacts(search=None, limit=200):
    _guard()
    conds, params = ["1=1"], {"lim": min(int(limit or 200), 500)}
    if search:
        conds.append("(c.first_name LIKE %(q)s OR c.last_name LIKE %(q)s OR c.email_id LIKE %(q)s OR c.mobile_no LIKE %(q)s)")
        params["q"] = f"%{search}%"
    rows = frappe.db.sql("""
        SELECT c.name,
               TRIM(CONCAT(IFNULL(c.first_name,''),' ',IFNULL(c.last_name,''))) AS full_name,
               c.email_id, c.mobile_no, c.phone, c.company_name, c.designation,
               dl.link_doctype, dl.link_name
        FROM `tabContact` c
        LEFT JOIN `tabDynamic Link` dl ON dl.parent=c.name AND dl.parenttype='Contact'
        WHERE {w}
        GROUP BY c.name
        ORDER BY c.modified DESC
        LIMIT %(lim)s
    """.format(w=" AND ".join(conds)), params, as_dict=True)
    return rows


@frappe.whitelist()
def save_contact(first_name, name=None, last_name=None, email=None, mobile=None,
                 company_name=None, designation=None, link_doctype=None, link_name=None):
    _guard()
    doc = frappe.get_doc("Contact", name) if name and frappe.db.exists("Contact", name) else frappe.new_doc("Contact")
    doc.first_name = cstr(first_name).strip()
    doc.last_name = cstr(last_name).strip()
    doc.company_name = cstr(company_name).strip()
    doc.designation = cstr(designation).strip()
    # Email / phone child tables (replace primary).
    doc.email_ids = []
    if email:
        doc.append("email_ids", {"email_id": cstr(email).strip(), "is_primary": 1})
    doc.phone_nos = []
    if mobile:
        doc.append("phone_nos", {"phone": cstr(mobile).strip(), "is_primary_mobile_no": 1})
    if link_doctype and link_name and not name:
        doc.append("links", {"link_doctype": link_doctype, "link_name": link_name})
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    return {"name": doc.name}


# ── Follow-ups & activities (ToDo + Comments) ────────────────────────────────
@frappe.whitelist()
def list_followups(status="Open", limit=100):
    _guard()
    filters = {}
    if status and status != "All":
        filters["status"] = status
    rows = frappe.get_all("ToDo", filters=filters,
                          fields=["name", "description", "date", "priority", "status",
                                  "reference_type", "reference_name", "allocated_to", "owner"],
                          order_by="field(priority,'High','Medium','Low'), date asc",
                          limit=int(limit or 100))
    return rows


@frappe.whitelist()
def create_followup(description, date=None, priority="Medium",
                    reference_doctype=None, reference_name=None, allocated_to=None):
    _guard()
    doc = frappe.get_doc({
        "doctype": "ToDo",
        "description": cstr(description).strip(),
        "date": date or today(),
        "priority": priority if priority in ("High", "Medium", "Low") else "Medium",
        "reference_type": reference_doctype or None,
        "reference_name": reference_name or None,
        "allocated_to": allocated_to or frappe.session.user,
        "assigned_by": frappe.session.user,
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return {"name": doc.name}


@frappe.whitelist()
def complete_followup(name):
    _guard()
    frappe.db.set_value("ToDo", name, "status", "Closed")
    return {"ok": True}


@frappe.whitelist()
def log_activity(reference_doctype, reference_name, content, activity_type="Note"):
    """Record an activity (note/call/meeting) as a timeline Comment on the record."""
    _guard()
    doc = frappe.get_doc({
        "doctype": "Comment",
        "comment_type": "Comment",
        "reference_doctype": reference_doctype,
        "reference_name": reference_name,
        "content": f"<b>{frappe.utils.escape_html(activity_type)}:</b> {frappe.utils.escape_html(content)}",
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return {"name": doc.name}


@frappe.whitelist()
def get_timeline(reference_doctype, reference_name):
    """Activities + follow-ups for a lead/customer/opportunity."""
    _guard()
    comments = frappe.get_all("Comment",
        filters={"reference_doctype": reference_doctype, "reference_name": reference_name,
                 "comment_type": "Comment"},
        fields=["content", "owner", "creation"], order_by="creation desc", limit=30)
    todos = frappe.get_all("ToDo",
        filters={"reference_type": reference_doctype, "reference_name": reference_name},
        fields=["name", "description", "date", "priority", "status"],
        order_by="creation desc", limit=30)
    return {"activities": comments, "followups": todos}


# ── Campaigns ────────────────────────────────────────────────────────────────
@frappe.whitelist()
def list_campaigns():
    _guard()
    return frappe.get_all("Campaign", fields=["name", "campaign_name", "description"],
                          order_by="modified desc", limit=100)


@frappe.whitelist()
def create_campaign(campaign_name, description=None):
    _guard()
    doc = frappe.get_doc({"doctype": "Campaign", "campaign_name": cstr(campaign_name).strip(),
                          "description": cstr(description or "").strip()})
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return {"name": doc.name}


# ── Lead import ──────────────────────────────────────────────────────────────
@frappe.whitelist()
def import_leads(rows, campaign=None, source=None):
    """Bulk-create leads. rows = list of {lead_name, email, mobile, company, source}."""
    _guard()
    if isinstance(rows, str):
        import json
        rows = json.loads(rows or "[]")
    created, skipped, errors = 0, 0, []
    for r in rows:
        name = cstr(r.get("lead_name") or r.get("name") or "").strip()
        email = cstr(r.get("email") or r.get("email_id") or "").strip()
        if not name and not email:
            skipped += 1
            continue
        try:
            if email and frappe.db.exists("Lead", {"email_id": email}):
                skipped += 1
                continue
            doc = frappe.get_doc({
                "doctype": "Lead",
                "lead_name": name or email,
                "email_id": email or None,
                "mobile_no": cstr(r.get("mobile") or r.get("mobile_no") or "").strip() or None,
                "company_name": cstr(r.get("company") or r.get("company_name") or "").strip() or None,
                "source": cstr(r.get("source") or source or "").strip() or None,
                "campaign_name": campaign or None,
            })
            doc.flags.ignore_permissions = True
            doc.insert(ignore_permissions=True)
            created += 1
        except Exception as e:
            errors.append(f"{name or email}: {e}")
    return {"created": created, "skipped": skipped, "errors": errors[:10]}


# ── WhatsApp outreach ────────────────────────────────────────────────────────
def _phone_recipients(audience, company=None, customer_group=None, territory=None, lead_status=None):
    """Resolve phone numbers for an audience (mirrors crm_outreach email logic)."""
    rows = []
    if audience in ("customers", "customers_outstanding"):
        conds, params = ["c.disabled=0"], {}
        if customer_group:
            conds.append("c.customer_group=%(cg)s"); params["cg"] = customer_group
        if territory:
            conds.append("c.territory=%(t)s"); params["t"] = territory
        oj = ""
        if audience == "customers_outstanding":
            oj = "JOIN (SELECT customer FROM `tabSales Invoice` WHERE docstatus=1 AND outstanding_amount>0 GROUP BY customer) si ON si.customer=c.name"
        rows = frappe.db.sql("""
            SELECT c.name, c.customer_name AS label, c.mobile_no AS phone
            FROM `tabCustomer` c {oj} WHERE {w} GROUP BY c.name
        """.format(oj=oj, w=" AND ".join(conds)), params, as_dict=True)
    elif audience == "leads":
        conds, params = ["IFNULL(mobile_no,'')!='' OR IFNULL(phone,'')!=''"], {}
        if lead_status and lead_status != "All":
            conds.append("status=%(s)s"); params["s"] = lead_status
        rows = frappe.db.sql("""
            SELECT name, lead_name AS label, COALESCE(NULLIF(mobile_no,''),phone) AS phone
            FROM `tabLead` WHERE {w}
        """.format(w=" AND ".join(conds)), params, as_dict=True)
    elif audience == "contacts":
        rows = frappe.db.sql("""
            SELECT name, TRIM(CONCAT(IFNULL(first_name,''),' ',IFNULL(last_name,''))) AS label,
                   COALESCE(NULLIF(mobile_no,''),phone) AS phone
            FROM `tabContact` WHERE IFNULL(mobile_no,'')!='' OR IFNULL(phone,'')!=''
        """, as_dict=True)

    seen, clean = set(), []
    no_phone = 0
    for r in rows:
        phone = cstr(r.get("phone") or "").strip()
        if not phone:
            no_phone += 1
            continue
        if phone in seen:
            continue
        seen.add(phone)
        clean.append({"name": r.get("name"), "label": r.get("label") or r.get("name"), "phone": phone})
    return clean, no_phone


@frappe.whitelist()
def preview_whatsapp(audience, company=None, customer_group=None, territory=None, lead_status=None):
    _guard()
    recips, no_phone = _phone_recipients(audience, company, customer_group, territory, lead_status)
    return {"count": len(recips), "no_phone_count": no_phone,
            "sample": [r["label"] for r in recips[:8]]}


def _send_whatsapp(number, message):
    """Create an outgoing WhatsApp Message (frappe_whatsapp dispatches on insert)."""
    doc = frappe.get_doc({
        "doctype": "WhatsApp Message",
        "type": "Outgoing",
        "to": cstr(number).strip(),
        "message": message,
        "content_type": "text",
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist()
def send_whatsapp_outreach(audience, message, company=None, customer_group=None,
                           territory=None, lead_status=None, test_number=None):
    _guard()
    message = cstr(message).strip()
    if not message:
        frappe.throw("Message is required.")

    if test_number:
        _send_whatsapp(test_number, message)
        return {"ok": True, "test": True, "sent": 1}

    recips, no_phone = _phone_recipients(audience, company, customer_group, territory, lead_status)
    if not recips:
        frappe.throw("No recipients with a phone number were found for this audience.")
    recips = recips[:1000]
    sent = 0
    for r in recips:
        try:
            _send_whatsapp(r["phone"], message.replace("{name}", r["label"]))
            sent += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"WhatsApp outreach to {r['phone']}")
    return {"ok": True, "sent": sent, "no_phone_count": no_phone}


# ── Sales Team ───────────────────────────────────────────────────────────────
@frappe.whitelist()
def get_sales_team(company=None):
    """Sales persons with sales attributed via the Sales Team table on invoices."""
    _guard()
    persons = frappe.get_all("Sales Person",
                             filters={"enabled": 1},
                             fields=["name", "sales_person_name", "commission_rate", "is_group"],
                             order_by="sales_person_name")
    co_cond = "AND si.company=%(co)s" if company else ""
    attributed = frappe.db.sql("""
        SELECT st.sales_person AS sp,
               IFNULL(SUM(si.base_grand_total * st.allocated_percentage / 100), 0) AS amount,
               COUNT(DISTINCT si.name) AS invoices
        FROM `tabSales Team` st
        INNER JOIN `tabSales Invoice` si ON si.name=st.parent AND st.parenttype='Sales Invoice'
        WHERE si.docstatus=1 {co}
        GROUP BY st.sales_person
    """.format(co=co_cond), {"co": company} if company else {}, as_dict=True)
    amap = {a.sp: a for a in attributed}
    out = []
    for p in persons:
        a = amap.get(p.name)
        out.append({
            "name": p.name,
            "sales_person_name": p.sales_person_name or p.name,
            "is_group": p.is_group,
            "commission_rate": flt(p.commission_rate),
            "total_sales": flt(a.amount) if a else 0,
            "invoices": (a.invoices if a else 0),
        })
    out.sort(key=lambda x: x["total_sales"], reverse=True)
    return out
