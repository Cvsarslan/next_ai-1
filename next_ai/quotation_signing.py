"""Native quotation email, e-signature, and acceptance workflow."""

import base64
import hashlib
import json
import secrets

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import add_days, cint, get_datetime, get_url, now_datetime, nowdate, validate_email_address
from frappe.utils.file_manager import save_file


def install_quotation_signing_fields():
    fields = [
        {"fieldname":"custom_signature_status","label":"Signature Status","fieldtype":"Select","options":"Not Sent\nSent\nSigned","default":"Not Sent","insert_after":"status","read_only":1},
        {"fieldname":"custom_signing_email","label":"Signing Email","fieldtype":"Data","options":"Email","insert_after":"custom_signature_status","read_only":1},
        {"fieldname":"custom_signing_token_hash","label":"Signing Token Hash","fieldtype":"Data","insert_after":"custom_signing_email","hidden":1,"read_only":1},
        {"fieldname":"custom_signing_expires","label":"Signing Link Expires","fieldtype":"Datetime","insert_after":"custom_signing_token_hash","read_only":1},
        {"fieldname":"custom_signature_name","label":"Signed By","fieldtype":"Data","insert_after":"custom_signing_expires","read_only":1},
        {"fieldname":"custom_signed_at","label":"Signed At","fieldtype":"Datetime","insert_after":"custom_signature_name","read_only":1},
        {"fieldname":"custom_signature_file","label":"Signature File","fieldtype":"Attach","insert_after":"custom_signed_at","read_only":1},
        {"fieldname":"custom_signed_ip","label":"Signer IP","fieldtype":"Data","insert_after":"custom_signature_file","read_only":1},
        {"fieldname":"custom_signed_sales_order","label":"Accepted Sales Order","fieldtype":"Link","options":"Sales Order","insert_after":"custom_signed_ip","read_only":1},
        {"fieldname":"custom_proforma_invoice","label":"Proforma Invoice","fieldtype":"Link","options":"Sales Invoice","insert_after":"custom_signed_sales_order","read_only":1},
    ]
    create_custom_fields({"Quotation": fields}, update=True)


def _token_hash(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


def _get_signed_quotation(token, require_open=True):
    token_hash = _token_hash(token)
    name = frappe.db.get_value("Quotation", {"custom_signing_token_hash":token_hash}, "name")
    if not name:
        frappe.throw("This signing link is invalid.", frappe.PermissionError)
    doc = frappe.get_doc("Quotation", name)
    if require_open:
        if doc.custom_signature_status == "Signed": frappe.throw("This quotation has already been signed.")
        if doc.custom_signing_expires and get_datetime(doc.custom_signing_expires) < now_datetime(): frappe.throw("This signing link has expired.")
        if doc.docstatus != 1: frappe.throw("This quotation is not available for signing.")
    return doc


@frappe.whitelist(methods=["POST"])
def send_quotation_for_signature(quotation, recipient=None, expires_in_days=14):
    doc = frappe.get_doc("Quotation", quotation)
    if not doc.has_permission("email"): frappe.throw("You do not have permission to send this quotation.", frappe.PermissionError)
    if doc.docstatus != 1: frappe.throw("Submit the quotation before sending it for signature.")
    recipient = recipient or doc.contact_email or frappe.db.get_value("Customer", doc.party_name, "email_id")
    recipient = (validate_email_address(recipient, throw=True) or [None])[0]
    token = secrets.token_urlsafe(32)
    expires = add_days(now_datetime(), max(1, min(cint(expires_in_days), 30)))
    doc.db_set({"custom_signature_status":"Sent","custom_signing_email":recipient,"custom_signing_token_hash":_token_hash(token),"custom_signing_expires":expires})
    link = get_url("/quotation-sign?token=" + token)
    message = f"""<p>Hello,</p><p>Please review and sign quotation <b>{frappe.utils.escape_html(doc.name)}</b>.</p><p><a href=\"{link}\" style=\"display:inline-block;padding:11px 18px;background:#0f766e;color:#fff;text-decoration:none;border-radius:7px\">Review and Sign Quotation</a></p><p>This secure link expires on {frappe.utils.format_datetime(expires)}.</p>"""
    frappe.sendmail(recipients=[recipient],subject=f"Signature requested: Quotation {doc.name}",message=message,attachments=[frappe.attach_print("Quotation",doc.name,print_format="NextAI Service Engagement",file_name=doc.name)],reference_doctype="Quotation",reference_name=doc.name)
    return {"ok":True,"recipient":recipient,"expires":str(expires),"signing_url":link}


@frappe.whitelist(allow_guest=True)
def get_quotation_for_signature(token):
    doc = _get_signed_quotation(token, require_open=False)
    return {"name":doc.name,"customer":doc.customer_name or doc.party_name,"date":doc.transaction_date,"valid_till":doc.valid_till,"currency":doc.currency,"grand_total":doc.grand_total,"status":doc.custom_signature_status,"expires":doc.custom_signing_expires,"items":[{"name":x.item_name or x.item_code,"description":frappe.utils.strip_html(x.description or ""),"qty":x.qty,"rate":x.rate,"amount":x.amount} for x in doc.items],"sales_order":doc.custom_signed_sales_order,"proforma_invoice":doc.custom_proforma_invoice}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def sign_quotation(token, signer_name, signature_data, accepted=0):
    if not cint(accepted): frappe.throw("You must accept the quotation terms before signing.")
    signer_name = (signer_name or "").strip()
    if len(signer_name) < 2: frappe.throw("Enter the signer's full name.")
    doc = _get_signed_quotation(token)
    frappe.db.sql("SELECT name FROM `tabQuotation` WHERE name=%s FOR UPDATE", doc.name)
    doc.reload()
    if doc.custom_signature_status == "Signed": frappe.throw("This quotation has already been signed.")
    prefix = "data:image/png;base64,"
    if not (signature_data or "").startswith(prefix): frappe.throw("Please provide a signature.")
    raw = base64.b64decode(signature_data[len(prefix):], validate=True)
    if len(raw) > 1024 * 1024: frappe.throw("Signature image is too large.")

    from erpnext.selling.doctype.quotation.quotation import make_sales_order
    so = make_sales_order(doc.name)
    for item in so.items: item.delivery_date = add_days(nowdate(), 7)
    so.flags.ignore_permissions = True
    so.insert(ignore_permissions=True)
    so.submit()
    from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
    invoice = make_sales_invoice(so.name, ignore_permissions=True)
    invoice.posting_date = nowdate()
    invoice.remarks = f"Proforma invoice generated from accepted quotation {doc.name}."
    invoice.flags.ignore_permissions = True
    invoice.insert(ignore_permissions=True)
    file_doc = save_file(f"signature-{doc.name}.png", raw, "Quotation", doc.name, is_private=1)
    ip = getattr(getattr(frappe.local, "request", None), "remote_addr", None) or ""
    doc.db_set({"custom_signature_status":"Signed","custom_signature_name":signer_name,"custom_signed_at":now_datetime(),"custom_signature_file":file_doc.file_url,"custom_signed_ip":ip,"custom_signed_sales_order":so.name,"custom_proforma_invoice":invoice.name})
    try:
        frappe.sendmail(recipients=[doc.custom_signing_email],subject=f"Quotation accepted - Proforma {invoice.name}",message=f"<p>Thank you for accepting quotation <b>{doc.name}</b>.</p><p>Your proforma invoice <b>{invoice.name}</b> is attached for payment.</p>",attachments=[frappe.attach_print("Sales Invoice",invoice.name,print_format="NextAI Proforma Invoice",file_name=invoice.name)],reference_doctype="Quotation",reference_name=doc.name)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Quotation signing: proforma email")
    return {"ok":True,"quotation":doc.name,"sales_order":so.name,"proforma_invoice":invoice.name}
