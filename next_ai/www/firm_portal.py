import frappe
from frappe.utils import getdate, nowdate


no_cache = 1


def get_context(context):
    context.no_breadcrumbs = True
    context.title = "Client Portal - Apex Ledger"
    context.is_guest = frappe.session.user == "Guest"
    context.currency = "AED"
    context.selected_company = ""
    context.current_year = int(getdate(nowdate()).year)
    context.csrf_token = ""

    if context.is_guest:
        return

    try:
        context.csrf_token = frappe.local.session.data.csrf_token or ""

        company = (
            frappe.defaults.get_user_default("Company")
            or frappe.db.get_single_value("Global Defaults", "default_company")
            or frappe.db.get_value("Company", {}, "name", order_by="name")
        )
        context.selected_company = str(company or "")
        if company:
            currency = frappe.db.get_value("Company", company, "default_currency")
            context.currency = str(currency or "AED")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Firm Portal: page context")
