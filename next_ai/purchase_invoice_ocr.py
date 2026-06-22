"""OCR extraction and account suggestions for Purchase Invoices."""

import base64
import json
import re

import frappe
from frappe import _
from frappe.utils import cstr, flt
from rapidfuzz import fuzz, process


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


@frappe.whitelist()
def scan_purchase_invoice(file_base64, filename, company):
	"""Extract invoice fields and suggest existing ERPNext masters."""
	if not frappe.has_permission("Purchase Invoice", ptype="create"):
		frappe.throw(_("You do not have permission to create Purchase Invoices."), frappe.PermissionError)
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("Please select a valid company before scanning."))
	if not frappe.has_permission("Company", doc=company):
		frappe.throw(_("You do not have access to company {0}.").format(frappe.bold(company)), frappe.PermissionError)

	media_type, encoded = _validate_file(file_base64, filename)
	data = _extract_invoice(encoded, media_type)
	return _enrich_extraction(data, company)


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
		frappe.throw(_("The invoice file must be 10 MB or smaller."))
	return media_type, encoded


def _extract_invoice(encoded, media_type):
	prompt = (
		"Extract this supplier invoice. Return only valid JSON with keys: "
		"supplier_name, supplier_tax_id, invoice_number, invoice_date (YYYY-MM-DD), "
		"due_date (YYYY-MM-DD), currency, subtotal, tax_amount, total, and items. "
		"Each item must contain description, category, quantity, unit_price, and net_amount. "
		"net_amount must exclude VAT/tax. Use null when unknown and never invent values."
	)
	client = _get_anthropic_client()
	if client:
		content_type = "document" if media_type == "application/pdf" else "image"
		message = client.messages.create(
			model=OCR_MODEL,
			max_tokens=1800,
			messages=[
				{
					"role": "user",
					"content": [
						{
							"type": content_type,
							"source": {
								"type": "base64",
								"media_type": media_type,
								"data": encoded,
							},
						},
						{"type": "text", "text": prompt},
					],
				}
			],
		)
		raw = "".join(getattr(block, "text", "") for block in message.content).strip()
	else:
		raw = _extract_invoice_with_gemini(encoded, media_type, prompt)

	match = re.search(r"\{.*\}", raw, re.DOTALL)
	if not match:
		frappe.throw(_("No invoice details could be found in this file."))
	try:
		return json.loads(match.group())
	except json.JSONDecodeError:
		frappe.throw(_("The scanned invoice response could not be read. Please try a clearer scan."))


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


def _extract_invoice_with_gemini(encoded, media_type, prompt):
	from google import genai
	from google.genai import types

	settings = frappe.get_single("NextAI Settings")
	api_key = settings.get_password("api_key")
	if not api_key or not settings.model_name:
		frappe.throw(_("Configure NextAI Settings or an Anthropic API key to scan Purchase Invoices."))

	client = genai.Client(api_key=api_key)
	response = client.models.generate_content(
		model=settings.model_name,
		contents=[
			types.Part.from_bytes(data=base64.b64decode(encoded), mime_type=media_type),
			prompt,
		],
		config=types.GenerateContentConfig(
			temperature=0,
			max_output_tokens=1800,
			response_mime_type="application/json",
		),
	)
	return cstr(response.text).strip()


def _enrich_extraction(data, company):
	supplier, supplier_score = _nearest_supplier(data.get("supplier_name"))
	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "root_type": "Expense", "is_group": 0, "disabled": 0},
		fields=["name", "account_name", "account_number"],
	)
	preferred_accounts = _supplier_expense_accounts(supplier)

	items = []
	for item in data.get("items") or []:
		description = cstr(item.get("description") or item.get("category") or _("Scanned invoice item"))
		account, score = _nearest_expense_account(
			f"{item.get('category') or ''} {description}", accounts, preferred_accounts
		)
		quantity = flt(item.get("quantity") or 1) or 1
		net_amount = flt(item.get("net_amount"))
		unit_price = flt(item.get("unit_price")) or (net_amount / quantity if net_amount else 0)
		items.append(
			{
				"description": description,
				"category": cstr(item.get("category")),
				"qty": quantity,
				"rate": unit_price,
				"net_amount": net_amount or quantity * unit_price,
				"expense_account": account,
				"expense_account_score": score,
			}
		)

	if not items and flt(data.get("subtotal") or data.get("total")):
		amount = flt(data.get("subtotal")) or flt(data.get("total")) - flt(data.get("tax_amount"))
		account, score = _nearest_expense_account(cstr(data.get("supplier_name")), accounts, preferred_accounts)
		items.append(
			{
				"description": cstr(data.get("supplier_name") or _("Scanned invoice")),
				"category": "",
				"qty": 1,
				"rate": amount,
				"net_amount": amount,
				"expense_account": account,
				"expense_account_score": score,
			}
		)

	return {
		"supplier": supplier,
		"supplier_match_score": supplier_score,
		"supplier_name": cstr(data.get("supplier_name")),
		"supplier_tax_id": cstr(data.get("supplier_tax_id")),
		"bill_no": cstr(data.get("invoice_number")),
		"bill_date": data.get("invoice_date"),
		"due_date": data.get("due_date"),
		"currency": cstr(data.get("currency")),
		"subtotal": flt(data.get("subtotal")),
		"tax_amount": flt(data.get("tax_amount")),
		"total": flt(data.get("total")),
		"tax_account": _purchase_tax_account(company),
		"items": items,
	}


def _nearest_supplier(supplier_name):
	if not supplier_name:
		return None, 0
	suppliers = frappe.get_all("Supplier", fields=["name", "supplier_name"])
	choices = {row.name: cstr(row.supplier_name or row.name) for row in suppliers}
	match = process.extractOne(cstr(supplier_name), choices, scorer=fuzz.WRatio)
	if not match or match[1] < 60:
		return None, round(match[1] / 100, 2) if match else 0
	return match[2], round(match[1] / 100, 2)


def _supplier_expense_accounts(supplier):
	if not supplier:
		return set()
	return set(
		frappe.db.sql_list(
			"""
			SELECT DISTINCT item.expense_account
			FROM `tabPurchase Invoice Item` item
			INNER JOIN `tabPurchase Invoice` invoice ON invoice.name = item.parent
			WHERE invoice.supplier = %s AND invoice.docstatus < 2
			  AND COALESCE(item.expense_account, '') != ''
			""",
			(supplier,),
		)
	)


def _nearest_expense_account(text, accounts, preferred_accounts=None):
	if not accounts:
		return None, 0
	search_text = _expand_account_terms(text)
	preferred_accounts = preferred_accounts or set()
	best_account = None
	best_score = -1
	for account in accounts:
		label = f"{account.account_name or ''} {account.account_number or ''}"
		expanded_label = _expand_account_terms(label)
		score = max(
			fuzz.WRatio(search_text, expanded_label),
			fuzz.token_set_ratio(search_text, expanded_label),
		)
		score += _account_semantic_bonus(search_text, expanded_label)
		if ("depreciation" in expanded_label or "amortization" in expanded_label) and not any(
			term in search_text for term in ("depreciation", "amortization", "fixed asset")
		):
			score -= 25
		if account.name in preferred_accounts:
			score = min(100, score + 8)
		if score > best_score:
			best_account, best_score = account.name, score
	return best_account, round(min(100, best_score) / 100, 2)


def _expand_account_terms(value):
	text = re.sub(r"[^a-z0-9]+", " ", cstr(value).lower()).strip()
	aliases = {
		"airfare": "air tickets travel flight",
		"conveyance": "taxi transport vehicle travel",
		"electricity": "utility utilities water electricity",
		"fuel": "vehicle petrol diesel transport",
		"internet": "communication telephone telecom hosting",
		"legal": "professional fees consultancy legal",
		"office supplies": "stationary stationery office supplies",
		"restaurant": "meal meals food kitchen",
		"stationary": "stationery office supplies",
		"stationery": "stationary office supplies",
		"taxi": "conveyance transport vehicle travel",
	}
	for phrase, expansion in aliases.items():
		if phrase in text:
			text = f"{text} {expansion}"
	return text


def _account_semantic_bonus(search_text, account_text):
	rules = (
		(("office supplies", "stationary", "stationery"), ("stationary", "stationery")),
		(("taxi", "cab"), ("conveyance", "transport")),
		(("flight", "airfare", "air ticket"), ("air ticket",)),
		(("hotel", "lodging", "accommodation"), ("hotel",)),
		(("meal", "food", "restaurant"), ("meal", "kitchen")),
		(("internet", "telecom", "phone"), ("communication", "telephone", "hosting")),
		(("fuel", "petrol", "diesel"), ("vehicle",)),
		(("electricity", "water bill"), ("electricity", "utility")),
	)
	for search_terms, account_terms in rules:
		if any(term in search_text for term in search_terms) and any(term in account_text for term in account_terms):
			return 35
	return 0


def _purchase_tax_account(company):
	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Tax", "is_group": 0, "disabled": 0},
		fields=["name", "account_name"],
	)
	if not accounts:
		return None

	def tax_score(account):
		label = cstr(account.account_name or account.name).lower()
		score = fuzz.WRatio("purchase input vat recoverable vat 5", label)
		if "vat" in label:
			score += 50
		if "input" in label or "recoverable" in label:
			score += 30
		if "5" in label:
			score += 10
		if any(term in label for term in ("excise", "exempt", "income tax", "output", "zero")):
			score -= 80
		return score

	match = max(accounts, key=tax_score)
	return match.name if tax_score(match) > 50 else None
