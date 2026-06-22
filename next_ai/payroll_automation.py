"""Automatic creation of month-end Payroll Entry drafts."""

from datetime import time

import frappe
from frappe.utils import get_first_day, get_last_day, get_time, getdate, now_datetime


DEFAULT_SHIFT_END = time(23, 59)


def create_month_end_payroll_drafts():
	"""Create one monthly draft per company after the final shift ends."""
	now = now_datetime()
	today = getdate(now)
	if today != getdate(get_last_day(today)) or now.time() < get_last_shift_end():
		return []

	created = []
	for company in frappe.get_all("Company", filters={"is_group": 0}, pluck="name"):
		entry = create_payroll_draft(company, get_first_day(today), today)
		if entry:
			created.append(entry.name)

	return created


def get_last_shift_end():
	"""Return the latest configured shift end, or the end-of-day fallback."""
	shift_end_times = frappe.get_all("Shift Type", pluck="end_time")
	return max((get_time(value) for value in shift_end_times if value is not None), default=DEFAULT_SHIFT_END)


def create_payroll_draft(company, start_date, end_date):
	"""Create an idempotent monthly Payroll Entry draft for a company."""
	filters = {
		"company": company,
		"start_date": start_date,
		"end_date": end_date,
		"payroll_frequency": "Monthly",
		"docstatus": ["<", 2],
	}
	if frappe.db.exists("Payroll Entry", filters):
		return None

	doc = frappe.get_doc(
		{
			"doctype": "Payroll Entry",
			"company": company,
			"start_date": start_date,
			"end_date": end_date,
			"payroll_frequency": "Monthly",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.fill_employee_details()
	doc.save(ignore_permissions=True)
	return doc
