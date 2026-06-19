import frappe
from frappe.model.document import Document
from frappe.utils import flt


class EmployeeLoanApplication(Document):
    def validate(self):
        if flt(self.requested_amount) <= 0:
            frappe.throw("Requested amount must be greater than zero.")
        if int(self.repayment_months or 0) <= 0:
            frappe.throw("Repayment period must be at least one month.")
        self.monthly_repayment = flt(self.requested_amount) / int(self.repayment_months)

