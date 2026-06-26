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

# ── Sales: item/supply categories with special UAE VAT treatment ─────────────
# Zero-rated (0%) supplies — VAT charged at 0%, still a taxable supply.
ZERO_RATED_SALES_KEYWORDS = {
    "export": "Exported goods are zero-rated when export evidence is retained.",
    "international transport": "International transport of passengers/goods is zero-rated.",
    "air transport": "International air transport is zero-rated.",
    "freight": "International freight / transport is typically zero-rated.",
    "healthcare": "Qualifying healthcare services are zero-rated.",
    "medical": "Qualifying medical/healthcare services & medicines are zero-rated.",
    "medicine": "Registered medicines and medical equipment are zero-rated.",
    "pharma": "Registered medicines are zero-rated.",
    "education": "Qualifying education services by recognised institutions are zero-rated.",
    "tuition": "Qualifying tuition by recognised institutions is zero-rated.",
    "school": "Qualifying school education is zero-rated.",
    "crude oil": "Crude oil and natural gas supplies are zero-rated.",
    "natural gas": "Natural gas supplies are zero-rated.",
    "investment gold": "Investment-grade precious metals (99%+) are zero-rated.",
    "investment silver": "Investment-grade precious metals (99%+) are zero-rated.",
    "first supply residential": "The first supply of a new residential building (within 3 years) is zero-rated.",
    "new residential": "The first supply of a new residential building (within 3 years) is zero-rated.",
}

# Exempt supplies — no VAT, and NOT a taxable supply (limits input recovery).
EXEMPT_SALES_KEYWORDS = {
    "residential rent": "Residential property leases are VAT-exempt.",
    "residential lease": "Residential property leases are VAT-exempt.",
    "residential tenancy": "Residential property leases are VAT-exempt.",
    "apartment rent": "Residential leases are VAT-exempt.",
    "bare land": "Supply of bare land is VAT-exempt.",
    "local passenger transport": "Local passenger transport is VAT-exempt.",
    "taxi": "Local passenger transport is VAT-exempt.",
    "bus fare": "Local passenger transport is VAT-exempt.",
    "interest": "Margin-based financial services (interest, loans) are VAT-exempt.",
    "loan ": "Margin-based financial services (interest, loans) are VAT-exempt.",
    "life insurance": "Life insurance and reinsurance are VAT-exempt.",
}

# Free zones designated for VAT (supplies inside/between them can be out of scope).
DESIGNATED_ZONE_KEYWORDS = (
    "designated zone", "free zone", "freezone", "jafza", "dafza", "kizad",
    "saif zone", "hamriyah", "rakez", "dmcc", "jebel ali free",
)


def _designated_zone_party(doctype, name):
    """Best-effort check if the party's address sits in a VAT designated zone."""
    if not name:
        return False
    try:
        rows = frappe.db.sql(
            """SELECT LOWER(CONCAT_WS(' ', IFNULL(a.address_line1,''), IFNULL(a.address_line2,''),
                      IFNULL(a.city,''), IFNULL(a.address_title,'')))
               FROM `tabAddress` a
               JOIN `tabDynamic Link` dl ON dl.parent = a.name AND dl.parenttype = 'Address'
               WHERE dl.link_doctype = %s AND dl.link_name = %s
               ORDER BY a.is_primary_address DESC, a.modified DESC LIMIT 3""",
            (doctype, name),
        )
        blob = " ".join((r[0] or "") for r in rows)
        return any(z in blob for z in DESIGNATED_ZONE_KEYWORDS)
    except Exception:
        return False


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
        item_text = _items_text(items, "income_account")
        in_designated_zone = _designated_zone_party("Customer", customer)
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

        # 3) Item description suggests an EXEMPT supply (checked before zero-rated:
        #    exempt is more restrictive and also limits input recovery).
        for kw, msg in EXEMPT_SALES_KEYWORDS.items():
            if kw in item_text:
                notes.append("Exempt supplies are not taxable — related input VAT may be irrecoverable.")
                return _result(
                    _code("EX", charge_vat=False),
                    severity="warn",
                    title="Likely exempt — do not charge VAT",
                    reason=msg + " Confirm the supply meets the exemption conditions before invoicing.",
                    notes=notes, currency=currency, registered=registered,
                )

        # 4) Item description suggests a ZERO-RATED supply (0%).
        for kw, msg in ZERO_RATED_SALES_KEYWORDS.items():
            if kw in item_text:
                notes.append("Retain the evidence required to support 0% rating.")
                return _result(
                    _code("ZR", charge_vat=True),
                    severity="warn",
                    title="Likely zero-rated (0%)",
                    reason=msg + " Verify the statutory conditions are met before applying 0%.",
                    notes=notes, currency=currency, registered=registered,
                )

        # 5) Customer in a VAT designated zone (free zone) → special rules.
        if in_designated_zone:
            notes.append("Confirm whether this is a supply of goods (possibly out of scope) "
                         "or services (usually standard-rated) within the designated zone.")
            return _result(
                _code("SR", charge_vat=True),
                severity="warn",
                title="Designated zone — verify treatment",
                reason=("The customer's address is in a VAT designated (free) zone. Certain supplies "
                        "of goods within/between designated zones are outside the scope of UAE VAT, "
                        "while services are generally standard-rated. Confirm the supply type."),
                notes=notes, currency=currency, registered=registered,
            )

        # 6) Domestic supply → standard rated by default.
        if not cust_country:
            notes.append("Customer country not set — defaulting to a domestic UAE supply.")
        if cust_trn:
            notes.append(f"Customer is VAT-registered (TRN {cust_trn}) — a valid tax invoice "
                         "showing your TRN and the VAT amount is required.")
        else:
            notes.append("No customer TRN on file (treated as B2C / unregistered) — still "
                         "standard-rated; issue a simplified tax invoice.")
        notes.append("Exempt (residential rent, margin-based financial services, local passenger "
                     "transport) and zero-rated (exports, qualifying healthcare/education, "
                     "investment metals) categories must be selected manually if they apply.")
        return _result(
            _code("SR", charge_vat=True),
            severity="ok",
            title="Charge standard VAT (5%)",
            reason=("Domestic supply to a UAE customer — standard-rated at 5% unless the supply "
                    "specifically qualifies as zero-rated or exempt based on the item/service."),
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
