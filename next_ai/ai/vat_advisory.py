"""Rule-based UAE VAT advisory engine for the client portal.

Given the context of a sales or purchase invoice being drafted, this module
advises:

  * Sales  — whether output VAT should be charged and which VAT code/treatment
             applies (standard, zero-rated export, out of scope, exempt …).
  * Purchase — whether input VAT is recoverable (and why not, when it isn't):
             blocked expense categories, missing supplier TRN, imports subject
             to reverse charge, and non-business / personal use.

This is deterministic guidance, not a legal opinion. The portal must always let
the user override the suggestion. Logic is intentionally conservative — when a
condition is uncertain it advises the user to verify rather than asserting a
favourable treatment.
"""

import frappe
from frappe.utils import flt, cstr

# ── UAE VAT codes ────────────────────────────────────────────────────────────
# Aligned with how the FTA VAT 201 return groups supplies/inputs.
VAT_CODES = {
    "SR":   {"label": "Standard Rated (5%)",        "rate": 5},
    "ZR":   {"label": "Zero Rated (0%)",            "rate": 0},
    "EX":   {"label": "Exempt",                     "rate": 0},
    "OS":   {"label": "Out of Scope",               "rate": 0},
    "RC":   {"label": "Reverse Charge",             "rate": 5},
    "BLK":  {"label": "Blocked Input (non-recoverable)", "rate": 5},
    "NONE": {"label": "No VAT",                     "rate": 0},
}

UAE_COUNTRY = "United Arab Emirates"

# GCC states that have implemented VAT. Supplies between implementing states
# follow special rules; this list lets us flag them rather than blindly treat
# everything non-UAE as a clean export.
GCC_IMPLEMENTING_STATES = {"Saudi Arabia", "Bahrain", "Oman"}

# Keywords that typically signal a *blocked* input (input VAT not recoverable
# under UAE VAT — e.g. entertainment, certain motor vehicles, employee perks).
BLOCKED_INPUT_KEYWORDS = {
    "entertainment": "Entertainment expenses are generally blocked from input VAT recovery.",
    "entertain": "Entertainment expenses are generally blocked from input VAT recovery.",
    "hospitality": "Hospitality / entertainment costs are generally non-recoverable.",
    "restaurant": "Meals & entertainment are generally non-recoverable.",
    "dining": "Meals & entertainment are generally non-recoverable.",
    "meal": "Meals & entertainment are generally non-recoverable.",
    "catering": "Catering for hospitality/entertainment is generally non-recoverable.",
    "gift": "Gifts are generally treated as entertainment and non-recoverable.",
    "personal": "Personal-use costs are not recoverable (business-use only).",
    "motor vehicle": "Motor vehicles available for personal use are blocked from recovery.",
    "car ": "A car available for personal use is blocked from input VAT recovery.",
    "vehicle": "A vehicle available for personal use may be blocked from recovery.",
}

# Keywords hinting an expense is personal / non-business in nature.
NON_BUSINESS_KEYWORDS = ("personal", "private", "family", "home use")


def _company_vat_status(company):
    """Return (registered, trn, currency) for the company."""
    if not company:
        return False, "", "AED"
    trn = frappe.get_value("Company", company, "tax_id")
    currency = frappe.get_value("Company", company, "default_currency") or "AED"
    return bool(trn), (trn or ""), currency


def _party_info(doctype, name):
    """Return (country, trn) for a Customer / Supplier, tolerant of missing data.

    Customer has no `country` field (country lives on the linked Address), while
    Supplier does — so we only read `country` directly when the field exists and
    otherwise fall back to the party's primary address.
    """
    if not name or not frappe.db.exists(doctype, name):
        return None, ""

    trn = frappe.db.get_value(doctype, name, "tax_id") or ""

    country = None
    if frappe.get_meta(doctype).has_field("country"):
        country = frappe.db.get_value(doctype, name, "country")
    if not country:
        rows = frappe.db.sql(
            """SELECT a.country
               FROM `tabAddress` a
               JOIN `tabDynamic Link` dl ON dl.parent = a.name AND dl.parenttype = 'Address'
               WHERE dl.link_doctype = %s AND dl.link_name = %s AND IFNULL(a.country, '') != ''
               ORDER BY a.is_primary_address DESC, a.modified DESC
               LIMIT 1""",
            (doctype, name),
        )
        country = rows[0][0] if rows else None

    return (country or None), (trn or "")


def _items_text(items, account_field):
    """Flatten item descriptions + chosen GL account names into searchable text."""
    parts = []
    for it in (items or []):
        for key in ("item_name", "item_code", "description"):
            if it.get(key):
                parts.append(cstr(it[key]))
        acct = it.get(account_field) or it.get("account")
        if acct:
            parts.append(cstr(acct))
    return " ".join(parts).lower()


def _code(code, **extra):
    base = {"code": code, "treatment": VAT_CODES[code]["label"], "rate": VAT_CODES[code]["rate"]}
    base.update(extra)
    return base


# ── Sales advisory ───────────────────────────────────────────────────────────
@frappe.whitelist()
def advise_sales_vat(customer=None, items=None, company=None):
    """Advise the output-VAT treatment for a sales invoice being drafted."""
    try:
        if isinstance(items, str):
            import json
            items = json.loads(items or "[]")
        if not company:
            from next_ai.api import _get_company
            company = _get_company()

        registered, _trn, currency = _company_vat_status(company)
        cust_country, cust_trn = _party_info("Customer", customer)
        notes = []

        # 1) Not VAT-registered → must not charge VAT.
        if not registered:
            return _result(
                _code("NONE", charge_vat=False),
                severity="warn",
                title="Do not charge VAT",
                reason=("Your company is not VAT-registered (no TRN on file), so you must "
                        "not charge VAT on this invoice. Check your registration obligation "
                        "if your taxable turnover is approaching AED 375,000."),
                currency=currency, registered=registered,
            )

        # 2) Registered + customer outside the UAE → export / place-of-supply.
        if cust_country and cust_country != UAE_COUNTRY:
            if cust_country in GCC_IMPLEMENTING_STATES:
                return _result(
                    _code("SR", charge_vat=True),
                    severity="warn",
                    title="GCC supply — verify treatment",
                    reason=(f"Customer is in {cust_country}, a GCC VAT-implementing state. "
                            "Intra-GCC supplies follow special place-of-supply rules — confirm "
                            "whether this is standard-rated, zero-rated, or your customer "
                            "accounts under reverse charge."),
                    currency=currency, registered=registered,
                )
            notes.append("Keep export/movement evidence to support zero-rating.")
            return _result(
                _code("ZR", charge_vat=True),
                severity="ok",
                title="Likely zero-rated export",
                reason=(f"Customer is outside the UAE ({cust_country}). Exports of goods, and many "
                        "services to non-residents, are zero-rated (0%) — but only if the statutory "
                        "conditions and evidence are met. Some services may be out of scope instead."),
                notes=notes, currency=currency, registered=registered,
            )

        # 3) Domestic supply → standard rated by default.
        if not cust_country:
            notes.append("Customer country not set — defaulting to a domestic UAE supply.")
        notes.append("Exempt (e.g. residential rent, certain financial services, local "
                     "passenger transport) and zero-rated (e.g. exports, certain healthcare/"
                     "education) categories need to be selected manually if they apply.")
        return _result(
            _code("SR", charge_vat=True),
            severity="ok",
            title="Charge standard VAT (5%)",
            reason=("Domestic supply to a UAE customer — standard-rated at 5% unless the supply "
                    "specifically qualifies as zero-rated or exempt."),
            notes=notes, currency=currency, registered=registered,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "VAT Advisory: sales")
        return _result(_code("SR", charge_vat=True), severity="ok",
                       title="Standard VAT (5%)", reason="Default UAE treatment.")


# ── Purchase advisory ────────────────────────────────────────────────────────
@frappe.whitelist()
def advise_purchase_vat(supplier=None, items=None, company=None):
    """Advise whether input VAT on a purchase/expense is recoverable, and why not."""
    try:
        if isinstance(items, str):
            import json
            items = json.loads(items or "[]")
        if not company:
            from next_ai.api import _get_company
            company = _get_company()

        registered, _trn, currency = _company_vat_status(company)
        supp_country, supp_trn = _party_info("Supplier", supplier)
        text = _items_text(items, "expense_account")
        notes = []

        # 1) Not VAT-registered → no input recovery at all.
        if not registered:
            return _result(
                _code("NONE", recoverable=False),
                severity="warn",
                title="Input VAT not recoverable",
                reason=("Your company is not VAT-registered (no TRN on file), so input VAT on "
                        "purchases cannot be recovered. Record the expense gross (VAT-inclusive)."),
                currency=currency, registered=registered,
            )

        # 2) Blocked expense categories (entertainment, personal vehicles, gifts…).
        for kw, msg in BLOCKED_INPUT_KEYWORDS.items():
            if kw in text:
                return _result(
                    _code("BLK", recoverable=False),
                    severity="err",
                    title="Blocked — input VAT not recoverable",
                    reason=msg + " Record the cost VAT-inclusive; do not claim the input tax.",
                    currency=currency, registered=registered,
                )

        # 3) Non-business / personal use.
        if any(kw in text for kw in NON_BUSINESS_KEYWORDS):
            return _result(
                _code("BLK", recoverable=False),
                severity="warn",
                title="Business-use only",
                reason=("This looks like a personal / non-business cost. Input VAT can only be "
                        "recovered to the extent the expense is used for taxable business supplies."),
                currency=currency, registered=registered,
            )

        # 4) Foreign supplier → reverse charge.
        if supp_country and supp_country != UAE_COUNTRY:
            return _result(
                _code("RC", recoverable=True),
                severity="warn",
                title="Reverse charge (import)",
                reason=(f"Supplier is outside the UAE ({supp_country}). Account for import VAT under "
                        "the reverse-charge mechanism — declare it as output tax and recover it as "
                        "input tax in the same return (where recoverable). The supplier does not "
                        "charge you UAE VAT."),
                currency=currency, registered=registered,
            )

        # 5) Domestic supplier with no TRN → no valid tax invoice.
        if not supp_trn:
            notes.append("Add the supplier's TRN to confirm a valid tax invoice was received.")
            return _result(
                _code("NONE", recoverable=False),
                severity="warn",
                title="No supplier TRN — verify tax invoice",
                reason=("No TRN is recorded for this supplier. Input VAT is only recoverable when you "
                        "hold a valid tax invoice from a VAT-registered supplier. Confirm the supplier's "
                        "TRN and that the invoice shows VAT before claiming it."),
                notes=notes, currency=currency, registered=registered,
            )

        # 6) Default — recoverable standard input VAT.
        return _result(
            _code("SR", recoverable=True),
            severity="ok",
            title="Recoverable input VAT (5%)",
            reason=("Domestic purchase from a registered supplier for business use — input VAT is "
                    "recoverable provided you hold a valid tax invoice."),
            currency=currency, registered=registered,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "VAT Advisory: purchase")
        return _result(_code("SR", recoverable=True), severity="ok",
                       title="Recoverable input VAT (5%)", reason="Default UAE treatment.")


def _result(code, severity="ok", title="", reason="", notes=None, currency="AED",
            registered=None):
    out = dict(code)
    out.update({
        "severity": severity,
        "title": title,
        "reason": reason,
        "notes": notes or [],
        "currency": currency,
    })
    if registered is not None:
        out["registered"] = registered
    return out
