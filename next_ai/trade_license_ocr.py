"""OCR extraction for UAE trade licenses.

Scans a trade-license image/PDF and returns the trade-license number, expiry date
and (best-effort) the legal name, TRN and Emirates ID, so the portal can pre-fill
the customer's UAE-compliance fields. Reuses the same LLM clients as the purchase
invoice OCR (Anthropic, with a Gemini fallback configured in NextAI Settings).
"""

import base64
import json
import re

import frappe
from frappe import _
from frappe.utils import cstr

OCR_MODEL = "claude-haiku-4-5-20251001"
MAX_FILE_SIZE = 10 * 1024 * 1024
SUPPORTED_MEDIA_TYPES = {
    "gif": "image/gif",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "pdf": "application/pdf",
    "png": "image/png",
    "webp": "image/webp",
}

_PROMPT = (
    "This is a UAE trade license (it may be in Arabic and English). Return only valid "
    "JSON with these keys: trade_license_number, trade_license_expiry (YYYY-MM-DD), "
    "company_name (the English legal name), trn (15-digit VAT number if shown), "
    "emirates_id (if shown). Use null for anything not present. Never invent values."
)


@frappe.whitelist()
def scan_trade_license(file_base64, filename):
    """Extract trade-license fields from an uploaded image/PDF."""
    if frappe.session.user in ("Guest", None, ""):
        frappe.throw(_("You must be signed in to scan a trade license."), frappe.PermissionError)

    media_type, encoded = _validate_file(file_base64, filename)
    data = _extract(encoded, media_type)

    # Normalise / sanity-check the extracted values.
    out = {
        "trade_license_number": _clean(data.get("trade_license_number")),
        "trade_license_expiry": _clean_date(data.get("trade_license_expiry")),
        "company_name": _clean(data.get("company_name")),
        "trn": _clean(data.get("trn")),
        "emirates_id": _clean(data.get("emirates_id")),
    }
    return out


def _validate_file(file_base64, filename):
    extension = cstr(filename).rsplit(".", 1)[-1].lower()
    media_type = SUPPORTED_MEDIA_TYPES.get(extension)
    if not media_type:
        frappe.throw(_("Upload a PDF, JPG, PNG, WEBP, or GIF file."))

    encoded = cstr(file_base64).split(",", 1)[-1]
    try:
        decoded_size = len(base64.b64decode(encoded, validate=True))
    except (ValueError, TypeError):
        frappe.throw(_("The uploaded file could not be read."))
    if decoded_size > MAX_FILE_SIZE:
        frappe.throw(_("The trade license file must be 10 MB or smaller."))
    return media_type, encoded


def _extract(encoded, media_type):
    client = _get_anthropic_client()
    if client:
        content_type = "document" if media_type == "application/pdf" else "image"
        message = client.messages.create(
            model=OCR_MODEL,
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": [
                    {"type": content_type, "source": {"type": "base64", "media_type": media_type, "data": encoded}},
                    {"type": "text", "text": _PROMPT},
                ],
            }],
        )
        raw = "".join(getattr(block, "text", "") for block in message.content).strip()
    else:
        raw = _extract_with_gemini(encoded, media_type)

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        frappe.throw(_("No trade-license details could be found in this file."))
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        frappe.throw(_("The scanned response could not be read. Please try a clearer scan."))


def _get_anthropic_client():
    import os
    try:
        import anthropic
    except ImportError:
        return None
    api_key = frappe.conf.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return anthropic.Anthropic(api_key=api_key)


def _extract_with_gemini(encoded, media_type):
    from google import genai
    from google.genai import types

    settings = frappe.get_single("NextAI Settings")
    api_key = settings.get_password("api_key")
    if not api_key or not settings.model_name:
        frappe.throw(_("Configure NextAI Settings or an Anthropic API key to scan trade licenses."))

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=settings.model_name,
        contents=[
            types.Part.from_bytes(data=base64.b64decode(encoded), mime_type=media_type),
            _PROMPT,
        ],
        config=types.GenerateContentConfig(
            temperature=0, max_output_tokens=800, response_mime_type="application/json",
        ),
    )
    return cstr(response.text).strip()


def _clean(value):
    v = cstr(value).strip()
    return v if v and v.lower() not in ("null", "none", "n/a") else None


def _clean_date(value):
    v = _clean(value)
    if not v:
        return None
    try:
        return cstr(frappe.utils.getdate(v))
    except Exception:
        return None
