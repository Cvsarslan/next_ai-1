"""Customer tax fields used by the firm portal."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


VAT_TREATMENTS = """VAT Registered
VAT Not Registered
GCC VAT Registered
GCC VAT Not Registered
Non-GCC
Designated Zone
Exempt
Out of Scope"""

# UAE FTA assigns staggered quarterly tax periods (plus monthly for large filers).
VAT_PERIOD_STAGGER = """Monthly
Quarterly (ending Mar, Jun, Sep, Dec)
Quarterly (ending Jan, Apr, Jul, Oct)
Quarterly (ending Feb, May, Aug, Nov)"""


def install_customer_tax_fields():
    create_custom_fields({
        "Customer": [
            {
                "fieldname": "custom_vat_treatment",
                "label": "VAT Treatment",
                "fieldtype": "Select",
                "options": VAT_TREATMENTS,
                "insert_after": "tax_id",
                "translatable": 0,
            },
            # ── UAE Compliance section ──────────────────────────────────
            {
                "fieldname": "custom_uae_compliance_section",
                "label": "UAE Compliance",
                "fieldtype": "Section Break",
                "insert_after": "custom_vat_treatment",
                "collapsible": 1,
            },
            {
                "fieldname": "custom_trade_license_number",
                "label": "Trade License Number",
                "fieldtype": "Data",
                "insert_after": "custom_uae_compliance_section",
                "translatable": 0,
            },
            {
                "fieldname": "custom_trade_license_expiry",
                "label": "Trade License Expiry",
                "fieldtype": "Date",
                "insert_after": "custom_trade_license_number",
            },
            {
                "fieldname": "custom_emirates_id",
                "label": "Emirates ID Number",
                "fieldtype": "Data",
                "insert_after": "custom_trade_license_expiry",
                "translatable": 0,
            },
            {
                "fieldname": "custom_uae_compliance_col1",
                "fieldtype": "Column Break",
                "insert_after": "custom_emirates_id",
            },
            {
                "fieldname": "custom_vat_number",
                "label": "VAT Number (TRN)",
                "fieldtype": "Data",
                "insert_after": "custom_uae_compliance_col1",
                "translatable": 0,
            },
            {
                "fieldname": "custom_vat_registration_date",
                "label": "VAT Registration Date",
                "fieldtype": "Date",
                "insert_after": "custom_vat_number",
            },
            {
                "fieldname": "custom_vat_period_stagger",
                "label": "VAT Period Stagger",
                "fieldtype": "Select",
                "options": VAT_PERIOD_STAGGER,
                "insert_after": "custom_vat_registration_date",
                "translatable": 0,
            },
            {
                "fieldname": "custom_uae_compliance_col2",
                "fieldtype": "Column Break",
                "insert_after": "custom_vat_period_stagger",
            },
            {
                "fieldname": "custom_corporate_tax_number",
                "label": "Corporate Tax Number",
                "fieldtype": "Data",
                "insert_after": "custom_uae_compliance_col2",
                "translatable": 0,
            },
            {
                "fieldname": "custom_corporate_tax_period_date",
                "label": "Corporate Tax Period Date",
                "fieldtype": "Date",
                "insert_after": "custom_corporate_tax_number",
            },
        ],
    }, update=True)
