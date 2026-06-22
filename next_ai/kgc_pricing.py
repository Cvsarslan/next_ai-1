# KGC General Pricing Structure (2026) as service Items + a quotation pricing
# assistant. Idempotent install; run via after_migrate or:
#   bench --site <site> execute next_ai.kgc_pricing.install_kgc_pricing
#
# Fixed-price services become catalogue Items. Tiered services (Audit by
# revenue, Bookkeeping by monthly transactions) are single Items whose rate is
# computed by get_kgc_pricing_quote() from the inputs collected on the quotation.

import frappe
import json
from frappe.utils import flt, cint

ITEM_GROUP = "KGC Services"

# code, name, default rate, scope label, billing note
FIXED_SERVICES = [
    ("KGC-VAT-REG", "VAT Registration / Deregistration", 1000, "VAT", "One Time"),
    ("KGC-CT-REG", "Corporate Tax Registration", 750, "Corporate Tax", "One Time"),
    ("KGC-VAT-RET", "VAT Return Filing", 1000, "VAT Return Filing", "Quarterly"),
    ("KGC-TRC", "Tax Residency Certificate (TRC)", 4500, "Tax Residency Certificate", "Yearly"),
    ("KGC-DOC", "Documentation & Agreement Drafting", 500, "Documentation", "Per Document"),
    ("KGC-CT-FILE", "Corporate Tax Filing - Simple Return", 2000, "Corporate Tax Filing", "Per Return"),
    ("KGC-TP", "Transfer Pricing Documentation", 6000, "Transfer Pricing", "Annually"),
    ("KGC-ICV", "ICV Certification Coordination", 4500, "ICV Certification", "Annually"),
    ("KGC-CBCR", "Country-by-Country Reporting (CbCR)", 5000, "CbCR", "Annually"),
    ("KGC-VAT-ADV", "VAT Advisory (2 hrs/month)", 1000, "VAT Advisory", "Monthly"),
    ("KGC-VAT-HC", "VAT Health Check", 10000, "VAT Health Check", "Yearly"),
]

# Tiered / computed services (rate filled by the assistant)
TIERED_SERVICES = [
    ("KGC-AUDIT", "Audit Report", 3000, "Audit", "Yearly"),
    ("KGC-BK", "Bookkeeping (Monthly)", 600, "Bookkeeping", "Monthly"),
    ("KGC-BK-VAT", "Bookkeeping + VAT Return Filing (Monthly)", 800, "Bookkeeping + VAT", "Monthly"),
    ("KGC-ONLINE", "Accounting Software Online Access", 1400, "Online Access", "Yearly"),
]

# ── Company Formation (Meydan Free Zone) ───────────────────────────────
# Representative Meydan FZ pricing — edit Item standard_rate to fine-tune.
FORMATION_PACKAGES = [
    ("MFZ-LIC-0", "Meydan FZ Commercial License (0 Visa)", 12500),
    ("MFZ-LIC-1", "Meydan FZ Commercial License (1 Visa)", 14900),
    ("MFZ-LIC-2", "Meydan FZ Commercial License (2 Visa)", 18500),
    ("MFZ-LIC-3", "Meydan FZ Commercial License (3 Visa)", 22000),
]
FORMATION_ADDONS = [
    # Visa & immigration
    ("MFZ-VISA", "Additional Employment Visa (2 years)", 3500),
    ("MFZ-VISA-INV", "Investor / Partner Visa (2 years)", 4000),
    ("MFZ-EID", "Medical + Emirates ID (per visa)", 1200),
    ("MFZ-ESTAB", "Establishment Card", 2000),
    ("MFZ-ECHANNEL", "E-Channel Registration", 1500),
    ("MFZ-VISA-CHG", "Status Change (in-country)", 750),
    ("MFZ-VISA-DEP", "Dependant / Family Visa", 3500),
    ("MFZ-VISA-CANCEL", "Visa Cancellation", 500),
    # License & activity
    ("MFZ-ACT", "Additional Business Activity", 1000),
    ("MFZ-REG", "Regulatory / External Approval", 0),
    ("MFZ-DUAL", "Dual License (Mainland Branch) Support", 5000),
    ("MFZ-NAME", "Trade Name Reservation", 500),
    ("MFZ-INIT", "Initial Approval", 500),
    # Office / facility
    ("MFZ-FLEXI", "Flexi-Desk (annual)", 6000),
    ("MFZ-OFFICE", "Physical Office Upgrade", 15000),
    ("MFZ-MEETING", "Meeting Room Package", 2000),
    # Documentation & PRO
    ("MFZ-PRO", "PRO / Document Clearing", 1500),
    ("MFZ-MOA", "MOA / LSA Drafting & Notarisation", 1500),
    ("MFZ-ATTEST", "Document Attestation", 1000),
    ("MFZ-TRANSLATE", "Legal Translation", 300),
    ("MFZ-POA", "Power of Attorney", 1000),
    # Banking & post-setup
    ("MFZ-BANK", "Corporate Bank Account Assistance", 2500),
    ("MFZ-CORPTAX", "Corporate Tax Registration", 750),
    ("MFZ-VATREG", "VAT Registration", 1000),
    ("MFZ-RENEW", "License Renewal Assistance", 1000),
    ("MFZ-COURIER", "Courier / Document Delivery", 150),
]
FORMATION_INCLUDED_ACTIVITIES = 3   # activities included before extra charge
FORMATION_MAX_ACTIVITIES = 10       # hard cap per license

# ── IFZA (Dubai) ───────────────────────────────────────────────────────
IFZA_PACKAGES = [
    ("IFZA-LIC-0", "IFZA License (0 Visa)", 12900),
    ("IFZA-LIC-1", "IFZA License (1 Visa)", 14900),
    ("IFZA-LIC-2", "IFZA License (2 Visa)", 17900),
    ("IFZA-LIC-3", "IFZA License (3 Visa)", 20900),
    ("IFZA-LIC-4", "IFZA License (4 Visa)", 24900),
]
IFZA_ADDONS = [
    # Visa & immigration
    ("IFZA-VISA", "Additional Employment Visa (2 years)", 3300),
    ("IFZA-VISA-INV", "Investor / Partner Visa (2 years)", 3800),
    ("IFZA-EID", "Medical + Emirates ID (per visa)", 1150),
    ("IFZA-ESTAB", "Establishment Card", 2000),
    ("IFZA-ECHANNEL", "E-Channel Registration", 1450),
    ("IFZA-VISA-CHG", "Status Change (in-country)", 700),
    ("IFZA-VISA-DEP", "Dependant / Family Visa", 3300),
    ("IFZA-VISA-CANCEL", "Visa Cancellation", 500),
    # License & activity
    ("IFZA-ACT", "Additional Business Activity", 1500),
    ("IFZA-REG", "Regulatory / External Approval", 0),
    ("IFZA-NAME", "Trade Name Reservation", 500),
    ("IFZA-INIT", "Initial Approval", 500),
    # Office / facility
    ("IFZA-FLEXI", "Flexi-Desk (annual)", 5500),
    ("IFZA-OFFICE", "Physical Office Upgrade", 14000),
    # Documentation & PRO
    ("IFZA-PRO", "PRO / Document Clearing", 1500),
    ("IFZA-MOA", "MOA Drafting & Notarisation", 1500),
    ("IFZA-ATTEST", "Document Attestation", 1000),
    ("IFZA-TRANSLATE", "Legal Translation", 300),
    ("IFZA-POA", "Power of Attorney", 1000),
    # Banking & post-setup
    ("IFZA-BANK", "Corporate Bank Account Assistance", 2500),
    ("IFZA-CORPTAX", "Corporate Tax Registration", 750),
    ("IFZA-VATREG", "VAT Registration", 1000),
    ("IFZA-RENEW", "License Renewal Assistance", 1000),
    ("IFZA-COURIER", "Courier / Document Delivery", 150),
]

# Jurisdiction registry — each free zone shares the activity master but has its
# own packages, add-ons and item-code prefixes.
JURISDICTIONS = {
    "meydan": {
        "name": "Meydan Free Zone",
        "packages": FORMATION_PACKAGES,
        "addons": FORMATION_ADDONS,
        "included": 3, "max": 10,
        "codes": {"act": "MFZ-ACT", "reg": "MFZ-REG", "visa": "MFZ-VISA", "eid": "MFZ-EID"},
    },
    "ifza": {
        "name": "IFZA (Dubai)",
        "packages": IFZA_PACKAGES,
        "addons": IFZA_ADDONS,
        "included": 3, "max": 50,
        "codes": {"act": "IFZA-ACT", "reg": "IFZA-REG", "visa": "IFZA-VISA", "eid": "IFZA-EID"},
    },
}


# Standard add-on template reused across the remaining free zones (suffix, name, base rate).
STANDARD_ADDONS = [
    ("VISA", "Additional Employment Visa (2 years)", 3500),
    ("VISA-INV", "Investor / Partner Visa (2 years)", 4000),
    ("EID", "Medical + Emirates ID (per visa)", 1200),
    ("ESTAB", "Establishment Card", 2000),
    ("ECHANNEL", "E-Channel Registration", 1500),
    ("VISA-CHG", "Status Change (in-country)", 750),
    ("VISA-DEP", "Dependant / Family Visa", 3500),
    ("VISA-CANCEL", "Visa Cancellation", 500),
    ("ACT", "Additional Business Activity", 1000),
    ("REG", "Regulatory / External Approval", 0),
    ("NAME", "Trade Name Reservation", 500),
    ("INIT", "Initial Approval", 500),
    ("FLEXI", "Flexi-Desk (annual)", 6000),
    ("OFFICE", "Physical Office Upgrade", 15000),
    ("PRO", "PRO / Document Clearing", 1500),
    ("MOA", "MOA Drafting & Notarisation", 1500),
    ("ATTEST", "Document Attestation", 1000),
    ("TRANSLATE", "Legal Translation", 300),
    ("POA", "Power of Attorney", 1000),
    ("BANK", "Corporate Bank Account Assistance", 2500),
    ("CORPTAX", "Corporate Tax Registration", 750),
    ("VATREG", "VAT Registration", 1000),
    ("RENEW", "License Renewal Assistance", 1000),
    ("COURIER", "Courier / Document Delivery", 150),
]


def _mk_addons(prefix, mult=1.0):
    return [("%s-%s" % (prefix, suf), name, int(round(rate * mult)) if rate else 0)
            for suf, name, rate in STANDARD_ADDONS]


def _mk_pkgs(prefix, label, rates):
    return [("%s-LIC-%d" % (prefix, i), "%s License (%d Visa)" % (label, i), r)
            for i, r in enumerate(rates)]


# Mainland license types (suffix, name, base fee) — visas added separately.
MAINLAND_TYPES = [
    ("SVC", "Service License", 13500),
    ("COM", "Commercial License", 15000),
    ("CIVIL", "Civil / Professional License", 12000),
    ("IND", "Industrial License", 20000),
]


def _mk_mainland_pkgs(prefix, mult=1.0):
    return [("%s-%s" % (prefix, suf), name, int(round(rate * mult)))
            for suf, name, rate in MAINLAND_TYPES]


def _mk_jur_mainland(name, prefix, mult=1.0):
    """Mainland jurisdiction: packages are license TYPES; visas via add-ons."""
    return {
        "name": name,
        "packages": _mk_mainland_pkgs(prefix, mult),
        "addons": _mk_addons(prefix, mult),
        "included": 3, "max": 10, "mainland": True,
        "codes": {"act": "%s-ACT" % prefix, "reg": "%s-REG" % prefix,
                  "visa": "%s-VISA" % prefix, "eid": "%s-EID" % prefix},
    }


def _mk_jur(name, prefix, label, rates, included=3, maxa=10, mult=1.0):
    return {
        "name": name,
        "packages": _mk_pkgs(prefix, label, rates),
        "addons": _mk_addons(prefix, mult),
        "included": included, "max": maxa,
        "codes": {"act": "%s-ACT" % prefix, "reg": "%s-REG" % prefix,
                  "visa": "%s-VISA" % prefix, "eid": "%s-EID" % prefix},
    }


# Additional UAE free zones & mainland — representative pricing (edit Item rates).
JURISDICTIONS.update({
    "dmcc":     _mk_jur("DMCC (Dubai)", "DMCC", "DMCC", [20000, 23000, 27000, 31000], maxa=6, mult=1.15),
    "dwtc":     _mk_jur("DWTC (Dubai World Trade Centre)", "DWTC", "DWTC", [15000, 18000, 21500, 25000]),
    "dwc":      _mk_jur("DWC / Dubai South", "DWC", "Dubai South", [13500, 16000, 19000, 22000]),
    "rakez":    _mk_jur("RAKEZ (Ras Al Khaimah)", "RAKEZ", "RAKEZ", [11000, 13500, 16000, 18500], mult=0.9),
    "ajman":    _mk_jur("Ajman Free Zone", "AJM", "Ajman FZ", [9000, 11500, 14000, 16500], mult=0.85),
    "abudhabi": _mk_jur("Abu Dhabi (KEZAD / ADGM)", "ADFZ", "Abu Dhabi FZ", [14000, 17000, 20000, 23500]),
    "ded":      _mk_jur_mainland("Dubai Mainland (DED)", "DED", mult=1.1),
    "admainland": _mk_jur_mainland("Abu Dhabi Mainland (ADDED)", "ADM", mult=1.05),
    "jafza":    _mk_jur("JAFZA (Jebel Ali)", "JAFZA", "JAFZA", [22000, 25000, 29000, 33000], maxa=5, mult=1.1),
    "dafza":    _mk_jur("DAFZA (Dubai Airport)", "DAFZA", "DAFZA", [23000, 26000, 30000, 34000], maxa=5, mult=1.15),
})


def _juris(jurisdiction):
    return JURISDICTIONS.get((jurisdiction or "meydan").lower(), JURISDICTIONS["meydan"])

# Regulated activities need external/government approval → extra fee (code → fee).
REGULATED_ACTIVITIES = {
    "6910.00": 5000,   # Legal Consultancy
    "7022.05": 3000,   # Financial Consultancy
    "6630.00": 5000,   # Investment Consultancy
    "6920.00": 2500,   # Tax & Accounting Consultancy
    "8690.05": 4000,   # Health & Wellness Consultancy
    "8550.00": 3500,   # Educational Consultancy
    "6820.00": 3000,   # Real Estate Consultancy
    "7810.00": 4000,   # Recruitment Consultancy
}

# Curated Meydan free-zone business activities with ISIC4-based codes, grouped
# into parent groups → child activities. Codes/structure are representative —
# adjust to match Meydan's current activity master. (code, name, group)
MEYDAN_ACTIVITIES = [
    # ── Consultancy ──
    ("7020.01", "Management Consultancy", "Consultancy"),
    ("7020.00", "Business Consultancy", "Consultancy"),
    ("7022.05", "Financial Consultancy", "Consultancy"),
    ("6920.00", "Tax & Accounting Consultancy", "Consultancy"),
    ("6910.00", "Legal Consultancy", "Consultancy"),
    ("6630.00", "Investment Consultancy", "Consultancy"),
    ("7110.00", "Engineering Consultancy", "Consultancy"),
    ("7490.07", "Project Management Consultancy", "Consultancy"),
    ("8550.00", "Educational Consultancy", "Consultancy"),
    ("8690.05", "Health & Wellness Consultancy", "Consultancy"),
    ("6820.00", "Real Estate Consultancy", "Consultancy"),
    ("7810.01", "HR Consultancy", "Consultancy"),
    ("7810.00", "Recruitment Consultancy", "Consultancy"),
    ("5229.05", "Logistics & Supply Chain Consultancy", "Consultancy"),
    ("8110.00", "Facilities Management Consultancy", "Consultancy"),
    # ── Information Technology ──
    ("6201.01", "IT Software Development", "Information Technology"),
    ("6201.04", "Mobile Application Development", "Information Technology"),
    ("6201.02", "Web Design & Development", "Information Technology"),
    ("6202.00", "IT Infrastructure & Network", "Information Technology"),
    ("6209.01", "Cybersecurity Services", "Information Technology"),
    ("6311.00", "Cloud & Data Hosting Services", "Information Technology"),
    ("6209.02", "AI & Data Analytics Services", "Information Technology"),
    ("6312.00", "Web Portals & Platforms", "Information Technology"),
    # ── Trading ──
    ("4690.01", "General Trading", "Trading"),
    ("4791.00", "E-Commerce Trading", "Trading"),
    ("4630.00", "Foodstuff Trading", "Trading"),
    ("4641.00", "Garments & Textiles Trading", "Trading"),
    ("4652.00", "Electronics Trading", "Trading"),
    ("4663.00", "Building Materials Trading", "Trading"),
    ("4649.05", "Cosmetics & Personal Care Trading", "Trading"),
    ("4773.01", "Jewellery Trading", "Trading"),
    ("4759.00", "Furniture Trading", "Trading"),
    ("4774.00", "Medical Equipment Trading", "Trading"),
    ("4541.00", "Auto Spare Parts Trading", "Trading"),
    ("4610.00", "Import & Export", "Trading"),
    # ── Marketing & Media ──
    ("7320.13", "Marketing Services", "Marketing & Media"),
    ("7310.00", "Advertising Services", "Marketing & Media"),
    ("7021.00", "Public Relations", "Marketing & Media"),
    ("7311.01", "Social Media Management", "Marketing & Media"),
    ("5819.00", "Content Creation Services", "Marketing & Media"),
    ("5911.00", "Media Production", "Marketing & Media"),
    ("7420.00", "Photography Services", "Marketing & Media"),
    ("5911.02", "Video Production & Editing", "Marketing & Media"),
    # ── Design & Creative ──
    ("7410.01", "Graphic Design", "Design & Creative"),
    ("7410.02", "Interior Design", "Design & Creative"),
    ("7410.03", "Product Design", "Design & Creative"),
    ("9001.00", "Event Production & Decor", "Design & Creative"),
    # ── Services ──
    ("8230.00", "Event Management", "Services"),
    ("7911.00", "Tourism & Travel Consultancy", "Services"),
    ("8121.00", "Cleaning Services Management", "Services"),
    ("9602.00", "Beauty & Grooming Services", "Services"),
    ("8559.00", "Training & Workshops", "Services"),
]
_ACT_NAME_BY_CODE = {c: n for c, n, g in MEYDAN_ACTIVITIES}

# Map internal groups → Meydan-style trade license categories (sectors).
GROUP_TO_CATEGORY = {
    "Consultancy": "Professional, Scientific & Technical",
    "Information Technology": "Information & Communication",
    "Trading": "Wholesale & Retail Trade",
    "Marketing & Media": "Information & Communication",
    "Design & Creative": "Arts, Entertainment & Recreation",
    "Services": "Administrative & Support Services",
}


def _ensure_item_group():
    if frappe.db.exists("Item Group", ITEM_GROUP):
        return ITEM_GROUP
    parent = frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ["in", ["", None]]}, "name") \
        or "All Item Groups"
    frappe.get_doc({"doctype": "Item Group", "item_group_name": ITEM_GROUP,
                    "parent_item_group": parent, "is_group": 0}).insert(ignore_permissions=True)
    return ITEM_GROUP


def _upsert_item(code, name, rate):
    if frappe.db.exists("Item", code):
        if flt(frappe.db.get_value("Item", code, "standard_rate")) != flt(rate):
            frappe.db.set_value("Item", code, "standard_rate", flt(rate))
        return code
    frappe.get_doc({
        "doctype": "Item",
        "item_code": code,
        "item_name": name,
        "item_group": ITEM_GROUP,
        "stock_uom": "Nos",
        "is_stock_item": 0,
        "is_sales_item": 1,
        "is_purchase_item": 0,
        "standard_rate": flt(rate),
        "description": name,
    }).insert(ignore_permissions=True)
    return code


def install_kgc_pricing():
    """Create/refresh all KGC service items. Safe to run repeatedly."""
    if not frappe.db.table_exists("Item"):
        return []
    _ensure_item_group()
    made = []
    for code, name, rate, scope, note in FIXED_SERVICES + TIERED_SERVICES:
        try:
            _upsert_item(code, name, rate)
            made.append(code)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "KGC pricing item %s" % code)
    for cfg in JURISDICTIONS.values():
        for code, name, rate in cfg["packages"] + cfg["addons"]:
            try:
                _upsert_item(code, name, rate)
                made.append(code)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "KGC formation item %s" % code)
    frappe.db.commit()
    return made


@frappe.whitelist()
def get_formation_jurisdictions():
    return [{"key": k, "name": v["name"]} for k, v in JURISDICTIONS.items()]


import os


def _activities_path(jur):
    return frappe.get_site_path("private", "files", "kgc_activities_%s.json" % (jur or "base"))


def _activities_for(jurisdiction=None):
    """Return the (code, name, group) activity list for a jurisdiction.
    Uses an imported per-jurisdiction file if present, else the shared base list."""
    jur = (jurisdiction or "").lower()
    if jur:
        path = _activities_path(jur)
        try:
            if os.path.exists(path):
                with open(path) as f:
                    rows = json.load(f)
                return [(r.get("code"), r.get("name"), r.get("group", "")) for r in rows if r.get("code")]
        except Exception:
            frappe.log_error(frappe.get_traceback(), "KGC read activities %s" % jur)
    return MEYDAN_ACTIVITIES


@frappe.whitelist()
def import_jurisdiction_activities(jurisdiction, activities):
    """Bulk-load a jurisdiction's real activity list.
    `activities` = JSON list of {code, name, group}. Persists per jurisdiction."""
    frappe.only_for(("System Manager", "Administrator"))
    if isinstance(activities, str):
        activities = json.loads(activities)
    rows = [{"code": str(a["code"]).strip(),
             "name": str(a.get("name", "")).strip(),
             "group": str(a.get("group", "")).strip()}
            for a in activities if a.get("code")]
    path = _activities_path((jurisdiction or "").lower())
    with open(path, "w") as f:
        json.dump(rows, f)
    return {"jurisdiction": jurisdiction, "count": len(rows)}


@frappe.whitelist()
def get_formation_activities(search=None, jurisdiction=None):
    """Jurisdiction activities table: code, name, category (sector), group code,
    and approval stage. Searchable by code / name / category."""
    q = (search or "").strip().lower()
    out = []
    for c, n, g in _activities_for(jurisdiction):
        category = GROUP_TO_CATEGORY.get(g, g)
        group_code = c.split(".")[0]
        regulated = c in REGULATED_ACTIVITIES
        if q and not (q in c.lower() or q in n.lower() or q in category.lower() or q in g.lower()):
            continue
        out.append({
            "code": c, "name": n, "group": g, "category": category,
            "group_code": group_code,
            "regulated": regulated, "approval_fee": REGULATED_ACTIVITIES.get(c, 0),
            "approval": "External Approval" if regulated else "Instant (FAWRI)",
        })
    return out


@frappe.whitelist()
def get_formation_categories(jurisdiction=None):
    """Distinct trade-license categories for the jurisdiction's activity filter."""
    cats = sorted({GROUP_TO_CATEGORY.get(g, g) for _, _, g in _activities_for(jurisdiction)})
    return cats


@frappe.whitelist()
def get_formation_catalogue(jurisdiction="meydan"):
    cfg = _juris(jurisdiction)
    act_code = cfg["codes"]["act"]
    return {
        "jurisdiction": cfg["name"],
        "jurisdiction_key": (jurisdiction or "meydan").lower(),
        "mainland": bool(cfg.get("mainland")),
        "packages": [{"code": c, "name": n, "rate": r} for c, n, r in cfg["packages"]],
        "addons": [{"code": c, "name": n, "rate": r} for c, n, r in cfg["addons"]],
        "included_activities": cfg["included"],
        "max_activities": cfg["max"],
        "extra_activity_fee": next((r for c, n, r in cfg["addons"] if c == act_code), 1000),
        "regulated": [{"code": c, "fee": f} for c, f in REGULATED_ACTIVITIES.items()],
    }


@frappe.whitelist()
def get_formation_quote(package=None, activities=None, extra_visas=0,
                        addons=None, eid_count=0, jurisdiction="meydan"):
    """Build Company Formation quotation lines from inputs for a jurisdiction."""
    install_kgc_pricing()
    cfg = _juris(jurisdiction)
    codes = cfg["codes"]
    if isinstance(activities, str):
        activities = json.loads(activities or "[]")
    if isinstance(addons, str):
        addons = json.loads(addons or "[]")
    activities = activities or []
    addons = addons or []

    pkg_map = {c: (n, r) for c, n, r in cfg["packages"]}
    addon_map = {c: (n, r) for c, n, r in cfg["addons"]}
    included, maxact = cfg["included"], cfg["max"]
    lines, notes = [], []

    if not package or package not in pkg_map:
        return {"lines": [], "notes": ["Select a %s license package." % cfg["name"]],
                "subtotal": 0, "vat": 0, "grand_total": 0}

    # Enforce the activity cap
    if len(activities) > maxact:
        return {"lines": [], "error": True,
                "notes": ["Maximum %d activities allowed per license. Please remove %d."
                          % (maxact, len(activities) - maxact)],
                "subtotal": 0, "vat": 0, "grand_total": 0}

    name, rate = pkg_map[package]
    act_disp = ["%s - %s" % (a, _ACT_NAME_BY_CODE.get(a, a)) for a in activities]
    act_label = (", ".join(act_disp[:5]) + (" …" if len(act_disp) > 5 else "")) if act_disp else "To be confirmed"
    lines.append({"item_code": package, "item_name": name, "qty": 1, "rate": rate,
                  "scope": "%s · Activities: %s" % (cfg["name"], act_label)})

    # Extra activities beyond the included allowance
    extra_acts = max(0, len(activities) - included)
    if extra_acts and codes["act"] in addon_map:
        an, ar = addon_map[codes["act"]]
        lines.append({"item_code": codes["act"], "item_name": an, "qty": extra_acts, "rate": ar,
                      "scope": "%d activity(ies) beyond %d included" % (extra_acts, included)})

    # Regulated activities → external/government approval fee per activity
    regulated = [a for a in activities if a in REGULATED_ACTIVITIES]
    for a in regulated:
        lines.append({"item_code": codes["reg"], "item_name": "Regulatory / External Approval",
                      "qty": 1, "rate": REGULATED_ACTIVITIES[a],
                      "scope": "Regulated: %s - %s" % (a, _ACT_NAME_BY_CODE.get(a, a))})
    if regulated:
        notes.append("Regulated activity selected (%d) — external approval fee applied." % len(regulated))

    if cint(extra_visas) > 0 and codes["visa"] in addon_map:
        vn, vr = addon_map[codes["visa"]]
        lines.append({"item_code": codes["visa"], "item_name": vn, "qty": cint(extra_visas), "rate": vr,
                      "scope": "Beyond package visa quota"})

    if cint(eid_count) > 0 and codes["eid"] in addon_map:
        en, er = addon_map[codes["eid"]]
        lines.append({"item_code": codes["eid"], "item_name": en, "qty": cint(eid_count), "rate": er,
                      "scope": "Per visa"})

    skip = {codes["visa"], codes["eid"], codes["act"], codes["reg"]}
    for code in addons:
        if code in skip:
            continue  # handled via dedicated counts
        if code in addon_map:
            an, ar = addon_map[code]
            lines.append({"item_code": code, "item_name": an, "qty": 1, "rate": ar, "scope": "Add-on"})

    if not activities:
        notes.append("No activities selected — confirm with the client before issuing.")

    subtotal = sum(flt(l["rate"]) * flt(l["qty"]) for l in lines)
    vat = round(subtotal * 0.05, 2)
    return {"lines": lines, "notes": notes, "subtotal": subtotal,
            "vat": vat, "grand_total": round(subtotal + vat, 2)}


# ── Tier logic ──────────────────────────────────────────────────────────
def _audit_rate(annual_revenue):
    rev = flt(annual_revenue)
    if rev <= 0:
        return None, "Enter annual revenue to price the audit."
    if rev <= 3_000_000:
        return 3000, None
    if rev <= 5_000_000:
        return 5000, None
    if rev <= 8_000_000:
        return 8000, None
    return None, "Revenue exceeds AED 8M — audit price needs manual review."


def _bookkeeping_rate(monthly_transactions, with_vat):
    txn = cint(monthly_transactions)
    if txn <= 0:
        return None, "Enter monthly transactions to price bookkeeping."
    tiers = [(25, 800), (50, 1200), (100, 1800), (200, 2800)] if with_vat \
        else [(25, 600), (50, 1000), (100, 1500), (200, 2500)]
    for limit, price in tiers:
        if txn <= limit:
            return price, None
    return None, "Above 200 transactions/month — package needs customisation."


@frappe.whitelist()
def get_pricing_catalogue():
    """Return the menu used to render the quotation pricing assistant."""
    return {
        "fixed": [{"code": c, "name": n, "rate": r, "scope": s, "note": note}
                  for c, n, r, s, note in FIXED_SERVICES],
        "audit_item": "KGC-AUDIT",
        "bookkeeping_item": "KGC-BK",
        "bookkeeping_vat_item": "KGC-BK-VAT",
        "online_item": "KGC-ONLINE",
        "online_rate": 1400,
    }


@frappe.whitelist()
def get_kgc_pricing_quote(services=None, doc_count=1, accounting_package="none",
                          monthly_transactions=None, include_audit=0,
                          annual_revenue=None, online_access=0, contract_months=12):
    """Build quotation lines from general inputs.

    services: JSON list of fixed-service codes.
    accounting_package: 'none' | 'bk' | 'bk_vat'.
    """
    install_kgc_pricing()  # ensure items exist
    if isinstance(services, str):
        services = json.loads(services or "[]")
    services = services or []
    by_code = {c: (n, r, s, note) for c, n, r, s, note in FIXED_SERVICES}
    lines, notes = [], []

    for code in services:
        if code not in by_code:
            continue
        name, rate, scope, note = by_code[code]
        qty = cint(doc_count) if code == "KGC-DOC" else 1
        lines.append({"item_code": code, "item_name": name, "qty": max(1, qty),
                      "rate": rate, "scope": "%s · %s" % (scope, note)})

    # Accounting / bookkeeping package
    if accounting_package in ("bk", "bk_vat"):
        with_vat = accounting_package == "bk_vat"
        rate, note = _bookkeeping_rate(monthly_transactions, with_vat)
        item = "KGC-BK-VAT" if with_vat else "KGC-BK"
        name = "Bookkeeping + VAT Return Filing (Monthly)" if with_vat else "Bookkeeping (Monthly)"
        if rate:
            months = max(1, cint(contract_months))
            lines.append({"item_code": item, "item_name": name, "qty": months,
                          "rate": rate, "scope": "%s txns/mo · %d month(s)" % (cint(monthly_transactions), months)})
        if note:
            notes.append(note)

    # Online access add-on
    if cint(online_access):
        lines.append({"item_code": "KGC-ONLINE", "item_name": "Accounting Software Online Access",
                      "qty": 1, "rate": 1400, "scope": "Yearly"})

    # Audit (revenue-tiered)
    if cint(include_audit):
        rate, note = _audit_rate(annual_revenue)
        if rate:
            lines.append({"item_code": "KGC-AUDIT", "item_name": "Audit Report",
                          "qty": 1, "rate": rate, "scope": "Revenue-based · Yearly"})
        if note:
            notes.append(note)

    subtotal = sum(flt(l["rate"]) * flt(l["qty"]) for l in lines)
    vat = round(subtotal * 0.05, 2)
    return {"lines": lines, "notes": notes, "subtotal": subtotal,
            "vat": vat, "grand_total": round(subtotal + vat, 2)}


# ── Service engagement: per-service required documents + terms ──────────
# Matched against quotation line item_codes (exact codes and/or code prefixes).
ENGAGEMENT = [
    {
        "key": "accounting", "label": "Accounting & Bookkeeping",
        "exact": ["KGC-BK"], "prefix": [],
        "documents": [
            "Valid trade license copy",
            "Bank statements for all accounts covering the period",
            "Sales invoices issued during the period",
            "Purchase / expense invoices and receipts",
            "Payroll / WPS records (if any)",
            "Opening balances / previous trial balance",
            "Fixed asset register (if applicable)",
        ],
        "terms": [
            "Fees are charged monthly based on the agreed transaction volume; actual volume is verified after data review.",
            "Source data must be provided in the agreed format and within the agreed cut-off dates.",
            "Turnaround time is 15–30 working days depending on transaction volume.",
            "Accounting software subscription is inclusive; Online Access portal is AED 1,400/year extra.",
            "Quarterly MIS (P&L, Balance Sheet, Cash Flow, Notes) is provided only where a formal engagement is in place.",
        ],
    },
    {
        "key": "accounting_vat", "label": "Accounting + VAT Return Filing",
        "exact": ["KGC-BK-VAT"], "prefix": [],
        "documents": [
            "Valid trade license copy",
            "VAT registration certificate (TRN)",
            "Bank statements for all accounts covering the period",
            "Sales invoices, credit/debit notes issued",
            "Purchase / expense invoices and import customs documents (if any)",
            "Previously filed VAT returns (if any)",
            "Opening balances / previous trial balance",
        ],
        "terms": [
            "Bookkeeping fees are monthly based on transaction volume; VAT returns are filed on a quarterly basis.",
            "The client is responsible for the completeness and accuracy of data provided.",
            "Any FTA penalties arising from late submission of data or incorrect information provided are the client's responsibility.",
            "Output and input VAT are reconciled before each filing; figures must be confirmed by the client prior to submission.",
            "Final tax invoice is subject to 5% VAT.",
        ],
    },
    {
        "key": "vat", "label": "VAT Services",
        "exact": ["KGC-VAT-REG", "KGC-VAT-RET", "KGC-VAT-ADV", "KGC-VAT-HC"], "prefix": [],
        "documents": [
            "Valid trade license copy",
            "Passport & Emirates ID of owner / partners",
            "Memorandum of Association (MOA)",
            "Bank account details / IBAN letter",
            "Turnover declaration / financial statements",
            "Sample sales and purchase invoices",
            "Customs registration details (if applicable)",
        ],
        "terms": [
            "VAT registration / deregistration timelines are subject to FTA processing.",
            "Monthly VAT advisory covers up to 2 hours per month; the annual VAT health check is performed once per year.",
            "The client remains responsible for the accuracy of underlying records and supporting documents.",
        ],
    },
    {
        "key": "corptax", "label": "Corporate Tax",
        "exact": ["KGC-CT-REG", "KGC-CT-FILE"], "prefix": [],
        "documents": [
            "Valid trade license copy",
            "MOA / AOA and any amendments",
            "Passport & Emirates ID of owners / partners",
            "Audited / management financial statements",
            "Accounting records for the financial period",
            "Previous Corporate Tax registration details (if any)",
        ],
        "terms": [
            "Corporate Tax simple-return filing applies where financial statements are already prepared.",
            "The client is responsible for the accuracy and completeness of financial data.",
            "FTA registration and filing deadlines apply; penalties for late action are the client's responsibility.",
        ],
    },
    {
        "key": "audit", "label": "Audit",
        "exact": ["KGC-AUDIT"], "prefix": [],
        "documents": [
            "Valid trade license and MOA (with amendments)",
            "Trial balance and general ledger for the year",
            "Bank statements and bank confirmation letters",
            "Sales and purchase invoices",
            "Fixed asset register and inventory listing",
            "Prior-year audited financial statements",
            "Lease agreements and payroll records",
        ],
        "terms": [
            "Audit fees are based on annual revenue tiers; revenue exceeding AED 8M is quoted after data review.",
            "Management is responsible for the preparation and fair presentation of the financial statements.",
            "The audit is conducted in accordance with International Standards on Auditing (ISA) and IFRS.",
            "A signed engagement letter is required before commencement of the audit.",
        ],
    },
    {
        "key": "trc", "label": "Tax Residency Certificate",
        "exact": ["KGC-TRC"], "prefix": [],
        "documents": [
            "Valid trade license and MOA",
            "Passport, Emirates ID and residence visa copies",
            "Certified bank statements (6 months)",
            "Tenancy contract / Ejari",
            "Audited financial statements",
        ],
        "terms": [
            "TRC issuance is subject to FTA approval and validity is one year.",
            "All supporting documents must be valid and, where required, attested.",
        ],
    },
    {
        "key": "tp", "label": "Transfer Pricing / CbCR / ICV",
        "exact": ["KGC-TP", "KGC-CBCR", "KGC-ICV"], "prefix": [],
        "documents": [
            "Group structure and ownership chart",
            "Intercompany agreements and transaction details",
            "Audited financial statements (group and entity)",
            "Functional and segmented financial data",
        ],
        "terms": [
            "Engagement is delivered in line with UAE / OECD transfer pricing and reporting rules.",
            "Benchmarking and documentation are based on data and access provided by the client.",
        ],
    },
    {
        "key": "formation", "label": "Company Formation",
        "exact": [], "prefix": ["MFZ-", "IFZA-", "DMCC-", "DWTC-", "DWC-", "RAKEZ-",
                                 "AJM-", "ADFZ-", "DED-", "ADM-", "JAFZA-", "DAFZA-"],
        "documents": [
            "Passport copies of all shareholders",
            "Passport-size photographs (white background)",
            "Emirates ID / residence visa copy (if UAE resident)",
            "Proof of address (utility bill / bank statement)",
            "Three proposed trade names (in order of preference)",
            "NOC from current sponsor (if on UAE employment visa)",
            "Parent company documents (for corporate shareholders)",
            "Brief business plan (for regulated / certain activities)",
        ],
        "terms": [
            "Government and authority fees are subject to change without prior notice.",
            "Timelines are subject to the relevant authority's approvals.",
            "Regulated activities require external / government approval at additional cost.",
            "Visa quota is subject to the selected office / facility package.",
            "Trade name approval is subject to availability and authority guidelines.",
            "Service fees are subject to 5% VAT; government fees are pass-through at actuals.",
        ],
    },
]


@frappe.whitelist()
def get_engagement_sections(item_codes):
    """Return matched engagement groups (documents + terms) for given line codes."""
    if isinstance(item_codes, str):
        item_codes = json.loads(item_codes)
    codes = [c for c in (item_codes or []) if c]
    out = []
    for g in ENGAGEMENT:
        hit = any(c in g["exact"] for c in codes) or \
              any(c.startswith(p) for c in codes for p in g["prefix"])
        if hit:
            out.append({"label": g["label"], "documents": g["documents"], "terms": g["terms"]})
    return out


# ── Service-proposal content (KGC 4-page format) ───────────────────────
# Accounting onboarding document checklist (category, required documents).
_ACC_DOC_TABLE = [
    ["Company Documents", "Trade Licence, MOA / AOA, VAT Certificate, Corporate Tax Registration Certificate, UBO details, ownership structure and regulatory approvals, if applicable."],
    ["Banking Records", "Monthly bank statements, cheque copies, transfer confirmations, payment receipts, bank facility documents and loan correspondence, if applicable."],
    ["Sales & Revenue", "Sales invoices, credit notes, customer contracts, customer ledgers, POS reports, payment gateway reports, e-commerce reports and revenue reconciliation details."],
    ["Purchases & Expenses", "Supplier invoices, purchase orders, expense bills, petty cash records, reimbursement claims, supplier statements and supplier agreements."],
    ["Payroll Records", "Salary sheets, WPS reports, employee contracts, employee master data, leave records, gratuity calculations and end-of-service records."],
    ["Assets & Inventory", "Fixed asset register, depreciation schedule, asset purchase invoices, inventory reports, stock movement details and inventory valuation reports."],
    ["VAT / Corporate Tax Records", "VAT returns, FTA correspondence, Corporate Tax registration details, prior submissions, tax payment receipts and tax authority notifications."],
    ["Prior Accounting Records", "Prior financial statements, trial balance, general ledger, management reports, audit reports, opening balances and reconciliation schedules."],
]
for _g in ENGAGEMENT:
    if _g["key"] in ("accounting", "accounting_vat"):
        _g["doc_table"] = _ACC_DOC_TABLE

# Four service pillars shown on the proposal cover.
PROPOSAL_PILLARS = [
    ("01", "Accounting", "Bookkeeping, reconciliations, financial reports and management accounts."),
    ("02", "VAT Compliance", "VAT registration, return preparation, review and FTA compliance support."),
    ("03", "Corporate Tax", "UAE Corporate Tax registration, filing support and compliance review."),
    ("04", "Advisory", "Audit support, business advisory, document review and compliance guidance."),
]
PROPOSAL_FEATURES = [
    ("Compliance Focused", "Structured support for UAE VAT, Corporate Tax, bookkeeping and audit readiness."),
    ("Client-Centric Process", "Clear document requirements, defined scope and transparent commercial terms."),
    ("Professional Delivery", "Practical accounting, tax and advisory support aligned with business requirements."),
]
PROPOSAL_ABOUT = ("provides professional accounting, bookkeeping, VAT, Corporate Tax, audit support and "
                  "business advisory services to UAE-based businesses. Our approach is focused on accurate "
                  "records, regulatory compliance, timely reporting and practical support for management decision-making.")
PROPOSAL_TAGLINE = "Accounting | VAT | Corporate Tax | Audit Support | Business Advisory"

ONBOARDING_STEPS = [
    ("1. Document Collection", "Client provides records and access to relevant accounting, tax and bank documents."),
    ("2. Initial Review", "KGC reviews completeness and identifies missing or unclear information."),
    ("3. Execution", "Services are performed based on agreed scope and available documents."),
    ("4. Delivery", "Final output, filing, report, or advisory conclusion is shared with the client."),
]

# Standard commercial terms (title, text) — page 4.
STANDARD_TERMS = [
    ("Proposal Validity", "This proposal remains valid until the validity date mentioned in the proposal, unless otherwise revised or withdrawn in writing."),
    ("Payment Terms", "Full payment is required in advance unless otherwise agreed in writing. For monthly or recurring services, invoices shall be issued at the beginning of each month or service period."),
    ("Turnaround Time", "Estimated completion timelines depend on the volume, complexity and completeness of records provided by the client."),
    ("Client Responsibility", "The client shall provide complete, accurate and timely documents. We shall not be liable for penalties, delays or compliance issues arising from incomplete, inaccurate or delayed information."),
    ("Tax Consultancy Exclusion", "Tax advisory, voluntary disclosure, reconsideration, tax assessment, complex VAT treatment, Corporate Tax position analysis and tax authority correspondence are excluded unless specifically mentioned in the scope."),
    ("Out-of-Scope Services", "Any additional work outside this proposal, including historical backlog, audit support, bank KYC response, regulatory clarification, or urgent filing, shall be billed separately after client confirmation."),
    ("Government Fees and Penalties", "Government fees, penalties, portal charges, bank charges and third-party costs are excluded unless expressly mentioned."),
    ("Confidentiality", "We shall maintain confidentiality of client information and documents, except where disclosure is required by law, regulation or competent authority."),
    ("Limitation of Liability", "Our responsibility is limited to the professional services expressly agreed in this proposal and shall not extend to indirect losses, consequential damages or matters outside the approved scope."),
    ("Acceptance", "Signing this proposal, confirming by email, or making payment shall be deemed acceptance of the scope, pricing and terms stated herein."),
]
