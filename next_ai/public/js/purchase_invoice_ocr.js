frappe.ui.form.on("Purchase Invoice", {
	refresh(frm) {
		if (frm.doc.docstatus === 0 && frm.doc.company && frappe.model.can_create("Purchase Invoice")) {
			frm.add_custom_button(__("Scan Invoice"), () => show_purchase_invoice_scanner(frm));
		}
	},
});

function show_purchase_invoice_scanner(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Scan Purchase Invoice"),
		fields: [
			{
				fieldname: "upload",
				fieldtype: "HTML",
				options: `
					<div class="purchase-invoice-ocr-upload">
						<p>${__("Upload a supplier invoice image or PDF. Extracted values will be shown for review before the form is changed.")}</p>
						<input type="file" class="form-control" accept="image/jpeg,image/png,image/webp,image/gif,application/pdf">
						<div class="ocr-status text-muted" style="margin-top: 12px;"></div>
					</div>`,
			},
		],
		primary_action_label: __("Scan"),
		primary_action: () => scan_purchase_invoice_file(frm, dialog),
	});
	dialog.show();
}

async function scan_purchase_invoice_file(frm, dialog) {
	const input = dialog.$wrapper.find("input[type=file]")[0];
	const file = input && input.files[0];
	if (!file) {
		frappe.msgprint(__("Choose an invoice image or PDF first."));
		return;
	}
	if (file.size > 10 * 1024 * 1024) {
		frappe.msgprint(__("The invoice file must be 10 MB or smaller."));
		return;
	}

	dialog.get_primary_btn().prop("disabled", true);
	dialog.$wrapper.find(".ocr-status").html(__("Scanning invoice and matching expense accounts..."));
	try {
		const file_base64 = await read_purchase_invoice_file(file);
		const response = await frappe.call({
			method: "next_ai.purchase_invoice_ocr.scan_purchase_invoice",
			args: { file_base64, filename: file.name, company: frm.doc.company },
			freeze: true,
			freeze_message: __("Reading purchase invoice..."),
		});
		show_purchase_invoice_review(frm, dialog, response.message || {});
	} catch (error) {
		dialog.get_primary_btn().prop("disabled", false);
		dialog.$wrapper.find(".ocr-status").html(
			`<span class="text-danger">${frappe.utils.escape_html(error.message || __("Invoice scan failed."))}</span>`,
		);
	}
}

function read_purchase_invoice_file(file) {
	return new Promise((resolve, reject) => {
		const reader = new FileReader();
		reader.onload = () => resolve(reader.result);
		reader.onerror = reject;
		reader.readAsDataURL(file);
	});
}

function show_purchase_invoice_review(frm, dialog, data) {
	const escape = frappe.utils.escape_html;
	const supplier = data.supplier
		? `${escape(data.supplier)} (${Math.round((data.supplier_match_score || 0) * 100)}% match)`
		: `${escape(data.supplier_name || __("Not detected"))} - ${__("no existing Supplier matched")}`;
	const rows = (data.items || []).map(
		(item) => `<tr>
			<td>${escape(item.description || "")}</td>
			<td class="text-right">${format_currency(item.net_amount, data.currency)}</td>
			<td>${escape(item.expense_account || __("No expense account found"))}<br>
				<small class="text-muted">${Math.round((item.expense_account_score || 0) * 100)}% ${__("match")}</small></td>
		</tr>`,
	).join("");

	dialog.$wrapper.find(".purchase-invoice-ocr-upload").html(`
		<div><b>${__("Supplier")}:</b> ${supplier}</div>
		<div><b>${__("Invoice No")}:</b> ${escape(data.bill_no || "-")} &nbsp; <b>${__("Date")}:</b> ${escape(data.bill_date || "-")}</div>
		<div><b>${__("Extracted Total")}:</b> ${format_currency(data.total, data.currency)} &nbsp; <b>${__("Tax")}:</b> ${format_currency(data.tax_amount, data.currency)}</div>
		<div class="table-responsive" style="margin-top: 12px;"><table class="table table-bordered table-sm">
			<thead><tr><th>${__("Description")}</th><th>${__("Net Amount")}</th><th>${__("Suggested Expense Head")}</th></tr></thead>
			<tbody>${rows || `<tr><td colspan="3">${__("No line items detected")}</td></tr>`}</tbody>
		</table></div>
		<p class="text-muted">${__("Review all OCR values and account suggestions before saving or submitting.")}</p>
	`);
	dialog.set_primary_action(__("Apply Details"), async () => {
		await apply_purchase_invoice_scan(frm, data);
		dialog.hide();
	});
	dialog.get_primary_btn().prop("disabled", false);
}

async function apply_purchase_invoice_scan(frm, data) {
	if (data.supplier) await frm.set_value("supplier", data.supplier);
	if (data.bill_no) await frm.set_value("bill_no", data.bill_no);
	if (data.bill_date) {
		await frm.set_value("bill_date", data.bill_date);
		await frm.set_value("posting_date", data.bill_date);
	}
	if (data.due_date) await frm.set_value("due_date", data.due_date);
	if (data.currency) await frm.set_value("currency", data.currency);

	frappe.model.clear_table(frm.doc, "items");
	(data.items || []).forEach((item) => {
		const row = frm.add_child("items");
		row.item_name = item.description || __("Scanned invoice item");
		row.description = item.description || row.item_name;
		row.qty = item.qty || 1;
		row.uom = "Nos";
		row.conversion_factor = 1;
		row.rate = item.rate || 0;
		row.expense_account = item.expense_account;
	});

	frappe.model.clear_table(frm.doc, "taxes");
	if (flt(data.tax_amount) && data.tax_account) {
		const tax = frm.add_child("taxes");
		tax.charge_type = "Actual";
		tax.account_head = data.tax_account;
		tax.description = __("Tax from scanned invoice");
		tax.tax_amount = data.tax_amount;
	}
	frm.refresh_fields(["items", "taxes"]);
	frm.dirty();
	frappe.show_alert({ message: __("Invoice details applied. Please review before saving."), indicator: "green" });
}
