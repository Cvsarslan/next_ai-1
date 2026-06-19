"""
Generic ERPNext action tools for the Click 2 Click Consultant AI.
Four tools cover every DocType and operation in ERPNext.
"""

import json
import frappe
from frappe import _
from frappe.utils import today


# ── Tool definitions sent to Gemini ─────────────────────────────────────────

TOOLS = [
    {
        "name": "create_document",
        "description": (
            "Create a new record in ANY ERPNext DocType. "
            "Use this for: Customer, Supplier, Item, Lead, Opportunity, Sales Order, "
            "Sales Invoice, Purchase Order, Purchase Invoice, Delivery Note, Purchase Receipt, "
            "Payment Entry, Journal Entry, Stock Entry, Employee, Leave Application, "
            "Expense Claim, Salary Slip, Work Order, BOM, Project, Task, and any other DocType. "
            "Pass all required fields in 'data'. For child table rows (e.g. items in an invoice), "
            "pass them as a list under the child table fieldname (e.g. 'items': [{...}, {...}])."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doctype": {
                    "type": "string",
                    "description": "Exact ERPNext DocType name, e.g. 'Sales Invoice', 'Customer', 'Item'"
                },
                "data": {
                    "type": "object",
                    "description": (
                        "Field values as a JSON object. Examples: "
                        "Customer: {customer_name, customer_type, customer_group, territory} | "
                        "Sales Invoice: {customer, posting_date, items:[{item_code,qty,rate}]} | "
                        "Sales Order: {customer, delivery_date, items:[{item_code,qty,rate}]} | "
                        "Purchase Order: {supplier, schedule_date, items:[{item_code,qty,rate}]} | "
                        "Payment Entry: {payment_type, party_type, party, paid_amount, paid_from, paid_to} | "
                        "Journal Entry: {voucher_type, accounts:[{account,debit_in_account_currency,credit_in_account_currency}]} | "
                        "Stock Entry: {stock_entry_type, items:[{item_code,qty,s_warehouse,t_warehouse}]} | "
                        "Employee: {first_name, last_name, gender, date_of_birth, date_of_joining, department, company} | "
                        "Item: {item_code, item_name, item_group, stock_uom} | "
                        "Lead: {lead_name, email_id, mobile_no, company_name} | "
                        "Project: {project_name, status, expected_start_date, expected_end_date} | "
                        "Task: {subject, project, status, priority}"
                    )
                },
            },
            "required": ["doctype", "data"],
        },
    },
    {
        "name": "update_document",
        "description": (
            "Update one or more fields on an existing ERPNext record. "
            "Use this to change values on any saved (draft or submitted) document. "
            "Examples: change a customer's territory, update a lead's status, "
            "modify item price, reschedule a delivery date, add a note to any record."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doctype": {
                    "type": "string",
                    "description": "Exact ERPNext DocType name"
                },
                "name": {
                    "type": "string",
                    "description": "The document name / ID to update, e.g. 'SINV-00001', 'John Doe'"
                },
                "data": {
                    "type": "object",
                    "description": "Fields to update as a JSON object, e.g. {status: 'Closed', territory: 'India'}"
                },
            },
            "required": ["doctype", "name", "data"],
        },
    },
    {
        "name": "submit_document",
        "description": (
            "Submit, cancel, or amend a document in ERPNext. "
            "Submit: finalises a draft document (Sales Invoice, Sales Order, Payment Entry, etc.). "
            "Cancel: reverses a submitted document. "
            "Amend: creates an editable copy of a cancelled document."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doctype": {
                    "type": "string",
                    "description": "Exact ERPNext DocType name"
                },
                "name": {
                    "type": "string",
                    "description": "Document name to act on, e.g. 'SINV-00001'"
                },
                "action": {
                    "type": "string",
                    "enum": ["submit", "cancel", "amend"],
                    "description": "'submit' to finalise, 'cancel' to reverse, 'amend' to edit a cancelled doc"
                },
            },
            "required": ["doctype", "name", "action"],
        },
    },
    {
        "name": "get_document",
        "description": (
            "Read a specific document or list records from any ERPNext DocType. "
            "Use with 'name' to fetch one record's full details. "
            "Omit 'name' and use 'filters' to search/list records. "
            "Examples: get a customer's details, list all open sales orders, "
            "find invoices for a specific customer, check stock levels, view employee list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doctype": {
                    "type": "string",
                    "description": "Exact ERPNext DocType name"
                },
                "name": {
                    "type": "string",
                    "description": "Document name for fetching a single record. Omit to list records."
                },
                "filters": {
                    "type": "string",
                    "description": (
                        "JSON filter array for listing, e.g. "
                        "[[\"status\",\"=\",\"Draft\"]] or "
                        "[[\"customer\",\"=\",\"John\"],[\"docstatus\",\"=\",1]]"
                    )
                },
                "fields": {
                    "type": "string",
                    "description": "JSON array of field names to return, e.g. [\"name\",\"status\",\"grand_total\"]. Default: [\"name\"]"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max records to return (default 10, max 50)"
                },
            },
            "required": ["doctype"],
        },
    },
]


# ── Dispatcher ───────────────────────────────────────────────────────────────

def run_action(tool_name: str, args: dict) -> dict:
    handlers = {
        "create_document":  _create_document,
        "update_document":  _update_document,
        "submit_document":  _submit_document,
        "get_document":     _get_document,
    }
    handler = handlers.get(tool_name)
    if not handler:
        return {"success": False, "error": f"Unknown action: {tool_name}"}
    try:
        return handler(**args)
    except frappe.exceptions.ValidationError as e:
        return {"success": False, "error": str(e)}
    except frappe.exceptions.DoesNotExistError as e:
        return {"success": False, "error": f"Record not found: {str(e)}"}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Click2Click AI action '{tool_name}' failed")
        return {"success": False, "error": str(e)}


def _normalize_data(data) -> dict:
    """Gemini sometimes returns data as a JSON string or list of pairs — normalise to dict."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return {}
    if isinstance(data, list):
        try:
            return dict(data)
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


def _default_company() -> str:
    return frappe.db.get_single_value("Global Defaults", "default_company") or ""


# DocTypes that need a company field auto-injected when missing
_NEEDS_COMPANY = {
    "Sales Invoice", "Sales Order", "Quotation", "Delivery Note",
    "Purchase Invoice", "Purchase Order", "Purchase Receipt", "Supplier Quotation",
    "Payment Entry", "Journal Entry", "Stock Entry", "Stock Reconciliation",
    "Expense Claim", "Salary Slip", "Leave Application", "Leave Allocation",
    "Attendance", "Employee", "Work Order", "BOM", "Production Plan",
    "Asset", "Asset Movement",
}


# ── Action handlers ──────────────────────────────────────────────────────────

def _create_document(doctype: str, data) -> dict:
    data = _normalize_data(data)

    # Auto-inject company for financial/HR documents
    if doctype in _NEEDS_COMPANY and not data.get("company"):
        data["company"] = _default_company()

    doc_data = {"doctype": doctype}
    doc_data.update(data)

    doc = frappe.get_doc(doc_data)

    # Auto-fill missing mandatory defaults where possible
    if hasattr(doc, "set_missing_values"):
        try:
            doc.set_missing_values()
        except Exception:
            pass

    doc.insert(ignore_permissions=True)

    url = f"/app/{_doctype_to_slug(doctype)}/{frappe.utils.cstr(doc.name)}"
    return {
        "success": True,
        "doctype": doctype,
        "name": doc.name,
        "url": url,
        "summary": f"{doctype} **{doc.name}** created successfully.",
    }


def _update_document(doctype: str, name: str, data) -> dict:
    data = _normalize_data(data)
    doc = frappe.get_doc(doctype, name)

    for field, value in data.items():
        doc.set(field, value)

    doc.save(ignore_permissions=True)

    url = f"/app/{_doctype_to_slug(doctype)}/{frappe.utils.cstr(doc.name)}"
    changed = ", ".join(f"{k}={v}" for k, v in data.items())
    return {
        "success": True,
        "doctype": doctype,
        "name": doc.name,
        "url": url,
        "summary": f"{doctype} **{doc.name}** updated: {changed}.",
    }


def _submit_document(doctype: str, name: str, action: str) -> dict:
    doc = frappe.get_doc(doctype, name)

    if action == "submit":
        doc.submit()
        msg = f"{doctype} **{doc.name}** submitted successfully."
    elif action == "cancel":
        doc.cancel()
        msg = f"{doctype} **{doc.name}** cancelled."
    elif action == "amend":
        amended = frappe.copy_doc(doc)
        amended.amended_from = doc.name
        amended.docstatus = 0
        amended.insert(ignore_permissions=True)
        name = amended.name
        msg = f"Amendment **{amended.name}** created from {doc.name}."
    else:
        return {"success": False, "error": f"Unknown action '{action}'. Use submit/cancel/amend."}

    url = f"/app/{_doctype_to_slug(doctype)}/{frappe.utils.cstr(name)}"
    return {
        "success": True,
        "doctype": doctype,
        "name": name,
        "url": url,
        "summary": msg,
    }


def _get_document(doctype: str, name: str = None, filters: str = None,
                  fields: str = None, limit: int = 10) -> dict:
    limit = min(int(limit or 10), 50)

    if name:
        # Single document fetch
        doc = frappe.get_doc(doctype, name)
        data = doc.as_dict()
        # Trim large fields to keep response manageable
        for key in list(data.keys()):
            val = data[key]
            if isinstance(val, str) and len(val) > 500:
                data[key] = val[:500] + "…"
        return {
            "success": True,
            "doctype": doctype,
            "name": name,
            "data": data,
            "summary": f"Fetched {doctype} **{name}**.",
        }

    # List fetch
    filter_list = []
    if filters:
        try:
            filter_list = json.loads(filters)
        except Exception:
            pass

    field_list = ["name"]
    if fields:
        try:
            field_list = json.loads(fields)
        except Exception:
            pass

    records = frappe.get_list(
        doctype,
        filters=filter_list,
        fields=field_list,
        limit_page_length=limit,
        ignore_permissions=True,
    )

    return {
        "success": True,
        "doctype": doctype,
        "count": len(records),
        "records": [dict(r) for r in records],
        "summary": (
            f"Found {len(records)} {doctype} record(s)."
            if records else f"No {doctype} records found."
        ),
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _doctype_to_slug(doctype: str) -> str:
    return doctype.lower().replace(" ", "-")
