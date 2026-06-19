import frappe
import json
from frappe.utils import cint, flt, cstr, nowdate, getdate
from next_ai.ai.uae_tax_knowledge import UAE_TAX_KNOWLEDGE, UAE_TAX_KNOWLEDGE_VERSION


PORTAL_AI_MODEL = "claude-opus-4-8"
_AI_MEMORY_TURNS = 50
_AI_CONTEXT_TURNS = 18


def _anthropic_client():
    """Return an Anthropic client if a key is configured, else None."""
    import os
    try:
        import anthropic
    except Exception:
        return None
    key = (frappe.conf.get("anthropic_api_key")
           or os.environ.get("ANTHROPIC_API_KEY")
           or "")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _gemini_llm(with_tools=False):
    """Use the provider already configured in NextAI Settings as portal fallback."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    settings = frappe.get_single("NextAI Settings")
    api_key = settings.get_password("api_key")
    if not api_key or not settings.model_name:
        return None
    llm = ChatGoogleGenerativeAI(
        model=settings.model_name,
        google_api_key=api_key,
        temperature=0.3,
        max_retries=2,
    )
    if not with_tools:
        return llm
    tools = [
        {"name": tool["name"], "description": tool["description"],
         "parameters": tool["input_schema"]}
        for tool in _AI_TOOLS
    ]
    return llm.bind_tools(tools)


def _gemini_message_text(response):
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else cstr(block)
            for block in content
        )
    return cstr(content)


def _portal_memory_key(user, company):
    import hashlib
    raw = f"{user}\0{company}".encode("utf-8")
    return "AIMEM-" + hashlib.sha256(raw).hexdigest()[:32]


def _get_portal_memory(company, create=True):
    user = frappe.session.user
    if not user or user == "Guest":
        frappe.throw("Sign in to use AI memory.", frappe.PermissionError)
    key = _portal_memory_key(user, company)
    if frappe.db.exists("NextAI Portal Memory", key):
        return frappe.get_doc("NextAI Portal Memory", key)
    if not create:
        return None
    doc = frappe.get_doc({
        "doctype": "NextAI Portal Memory",
        "memory_key": key,
        "user": user,
        "company": company,
        "memory_notes": "",
        "chat_history": "[]",
        "agent_history": "[]",
        "last_interaction": frappe.utils.now_datetime(),
    })
    doc.insert(ignore_permissions=True)
    return doc


def _memory_history(doc, mode):
    field = "agent_history" if mode == "agent" else "chat_history"
    try:
        rows = json.loads(doc.get(field) or "[]")
    except (TypeError, ValueError):
        rows = []
    return [row for row in rows if row.get("role") in ("user", "assistant") and row.get("content")]


def _save_memory_history(doc, mode, rows):
    field = "agent_history" if mode == "agent" else "chat_history"
    doc.set(field, json.dumps(rows[-_AI_MEMORY_TURNS:], ensure_ascii=True))
    doc.last_interaction = frappe.utils.now_datetime()
    doc.save(ignore_permissions=True)


def _record_agent_action(company, text):
    doc = _get_portal_memory(company)
    rows = _memory_history(doc, "agent")
    _save_memory_history(doc, "agent", rows + [{"role": "assistant", "content": text}])


def _remember_explicit_fact(doc, message):
    """Store facts only when the user explicitly asks the assistant to remember them."""
    import re
    match = re.search(r"(?is)\b(?:please\s+)?remember(?:\s+that)?\s*[:,-]?\s*(.+)", message or "")
    if not match:
        return
    fact = " ".join(match.group(1).strip().split())[:500]
    if not fact:
        return
    notes = [line[2:] for line in (doc.memory_notes or "").splitlines() if line.startswith("- ")]
    if fact.lower() not in {note.lower() for note in notes}:
        notes.append(fact)
    doc.memory_notes = "\n".join(f"- {note}" for note in notes[-25:])


@frappe.whitelist()
def get_portal_ai_memory(company=None, mode="chat"):
    company = company or _get_company()
    doc = _get_portal_memory(company)
    return {
        "history": _memory_history(doc, mode),
        "memory_notes": doc.memory_notes or "",
        "tax_knowledge_version": UAE_TAX_KNOWLEDGE_VERSION,
    }


@frappe.whitelist(methods=["POST"])
def clear_portal_ai_memory(company=None, mode=None):
    company = company or _get_company()
    doc = _get_portal_memory(company)
    updates = {"last_interaction": frappe.utils.now_datetime()}
    if mode in ("chat", "agent"):
        updates["agent_history" if mode == "agent" else "chat_history"] = "[]"
    else:
        updates.update({"chat_history": "[]", "agent_history": "[]", "memory_notes": ""})
    frappe.db.set_value("NextAI Portal Memory", doc.name, updates, update_modified=True)
    return {"ok": True}


@frappe.whitelist(methods=["POST"])
def record_portal_ai_outcome(outcome, company=None):
    company = company or _get_company()
    text = " ".join(cstr(outcome).split())[:500]
    if text:
        _record_agent_action(company, text)
    return {"ok": True}


def _portal_financial_context(company):
    """Compact live financial snapshot for the AI system prompt."""
    parts = []
    try:
        row = frappe.db.sql("""
            SELECT
              (SELECT IFNULL(SUM(grand_total),0) FROM `tabSales Invoice`    WHERE docstatus=1 AND company=%(co)s) AS sales,
              (SELECT IFNULL(SUM(grand_total),0) FROM `tabPurchase Invoice` WHERE docstatus=1 AND company=%(co)s) AS purchases,
              (SELECT IFNULL(SUM(outstanding_amount),0) FROM `tabSales Invoice`    WHERE docstatus=1 AND outstanding_amount>0 AND company=%(co)s) AS receivables,
              (SELECT IFNULL(SUM(outstanding_amount),0) FROM `tabPurchase Invoice` WHERE docstatus=1 AND outstanding_amount>0 AND company=%(co)s) AS payables,
              (SELECT COUNT(*) FROM `tabCustomer` WHERE disabled=0) AS customers,
              (SELECT COUNT(*) FROM `tabSupplier` WHERE disabled=0) AS suppliers
        """, {"co": company}, as_dict=True)
        if row:
            r = row[0]
            parts.append(
                f"Total sales: {flt(r.sales):,.2f}; total purchases: {flt(r.purchases):,.2f}; "
                f"receivables: {flt(r.receivables):,.2f}; payables: {flt(r.payables):,.2f}; "
                f"{int(r.customers)} customers; {int(r.suppliers)} suppliers."
            )
    except Exception:
        pass
    return " ".join(parts) or "No financial data available yet."


# ── Agent tool definitions ───────────────────────────────────────
# Read-only tools execute inline and feed results back to Claude.
# create_* tools are intercepted and surfaced to the user for confirmation.
_AI_ITEM_SCHEMA = {
    "type": "array",
    "description": "Line items.",
    "items": {
        "type": "object",
        "properties": {
            "item_name": {"type": "string", "description": "Item name or description"},
            "item_code": {"type": "string", "description": "Existing item code, if known"},
            "qty": {"type": "number"},
            "rate": {"type": "number", "description": "Unit price"},
        },
        "required": ["item_name", "qty", "rate"],
    },
}

_AI_TOOLS = [
    {"name": "get_financials", "description": "Get a financial summary: sales, purchases, receivables, payables, and bank/cash balances.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "find_customers", "description": "Search customers by name. Returns matching customer names.",
     "input_schema": {"type": "object", "properties": {"search": {"type": "string"}}, "required": ["search"]}},
    {"name": "find_suppliers", "description": "Search suppliers by name.",
     "input_schema": {"type": "object", "properties": {"search": {"type": "string"}}, "required": ["search"]}},
    {"name": "find_items", "description": "Search items by name or code.",
     "input_schema": {"type": "object", "properties": {"search": {"type": "string"}}, "required": ["search"]}},
    {"name": "list_recent_invoices", "description": "List recent sales invoices with customer, amount, and status.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "create_customer", "description": "Create a new customer.",
     "input_schema": {"type": "object", "properties": {
         "customer_name": {"type": "string"}, "mobile_no": {"type": "string"}, "email_id": {"type": "string"}},
         "required": ["customer_name"]}},
    {"name": "create_supplier", "description": "Create a new supplier/vendor.",
     "input_schema": {"type": "object", "properties": {
         "supplier_name": {"type": "string"}, "mobile_no": {"type": "string"}, "email_id": {"type": "string"}},
         "required": ["supplier_name"]}},
    {"name": "create_sales_invoice", "description": "Create a sales invoice for a customer.",
     "input_schema": {"type": "object", "properties": {
         "customer": {"type": "string", "description": "Exact customer name"},
         "items": _AI_ITEM_SCHEMA, "due_date": {"type": "string", "description": "YYYY-MM-DD"}},
         "required": ["customer", "items"]}},
    {"name": "create_sales_order", "description": "Create a sales order for a customer.",
     "input_schema": {"type": "object", "properties": {
         "customer": {"type": "string"}, "items": _AI_ITEM_SCHEMA,
         "delivery_date": {"type": "string", "description": "YYYY-MM-DD"}},
         "required": ["customer", "items"]}},
    {"name": "create_quotation", "description": "Create a quotation for a customer.",
     "input_schema": {"type": "object", "properties": {
         "customer": {"type": "string"}, "items": _AI_ITEM_SCHEMA,
         "valid_till": {"type": "string", "description": "YYYY-MM-DD"}},
         "required": ["customer", "items"]}},
    {"name": "create_purchase_order", "description": "Create a purchase order for a supplier.",
     "input_schema": {"type": "object", "properties": {
         "supplier": {"type": "string"}, "items": _AI_ITEM_SCHEMA,
         "schedule_date": {"type": "string", "description": "YYYY-MM-DD"}},
         "required": ["supplier", "items"]}},
    {"name": "create_purchase_invoice", "description": "Create a purchase invoice (bill) from a supplier.",
     "input_schema": {"type": "object", "properties": {
         "supplier": {"type": "string"}, "items": _AI_ITEM_SCHEMA, "bill_no": {"type": "string"}},
         "required": ["supplier", "items"]}},
    {"name": "get_document", "description": "Read one ERPNext document or list documents from any permitted DocType.",
     "input_schema": {"type": "object", "properties": {
         "doctype": {"type": "string"}, "name": {"type": "string"},
         "filters": {"type": "string", "description": "JSON filter array"},
         "fields": {"type": "string", "description": "JSON field-name array"},
         "limit": {"type": "integer"}}, "required": ["doctype"]}},
    {"name": "create_document", "description": "Create a draft document in any permitted ERPNext DocType. Use exact fieldnames and child-table arrays.",
     "input_schema": {"type": "object", "properties": {
         "doctype": {"type": "string"}, "data": {"type": "object"}},
         "required": ["doctype", "data"]}},
    {"name": "update_document", "description": "Update fields on an existing permitted ERPNext document.",
     "input_schema": {"type": "object", "properties": {
         "doctype": {"type": "string"}, "name": {"type": "string"}, "data": {"type": "object"}},
         "required": ["doctype", "name", "data"]}},
    {"name": "submit_document", "description": "Submit, cancel, or amend an existing permitted ERPNext document.",
     "input_schema": {"type": "object", "properties": {
         "doctype": {"type": "string"}, "name": {"type": "string"},
         "action": {"type": "string", "enum": ["submit", "cancel", "amend"]}},
         "required": ["doctype", "name", "action"]}},
]

_AI_WRITE_TOOLS = {
    t["name"] for t in _AI_TOOLS
    if t["name"].startswith("create_") or t["name"] in ("update_document", "submit_document")
}


def _ai_run_readonly_tool(name, inp, company):
    """Execute a read-only agent tool and return a JSON-serialisable result."""
    try:
        if name == "get_financials":
            return _portal_financial_context(company)
        if name == "find_customers":
            rows = frappe.get_all("Customer", filters={"customer_name": ["like", f"%{inp.get('search','')}%"]},
                                  fields=["name", "customer_name"], limit=10)
            return rows or "No matching customers."
        if name == "find_suppliers":
            rows = frappe.get_all("Supplier", filters={"supplier_name": ["like", f"%{inp.get('search','')}%"]},
                                  fields=["name", "supplier_name"], limit=10)
            return rows or "No matching suppliers."
        if name == "find_items":
            s = inp.get("search", "")
            rows = frappe.get_all("Item", or_filters={"item_name": ["like", f"%{s}%"], "item_code": ["like", f"%{s}%"]},
                                  fields=["item_code", "item_name", "standard_rate"], limit=10)
            return rows or "No matching items."
        if name == "list_recent_invoices":
            rows = frappe.get_all("Sales Invoice", filters={"docstatus": 1, "company": company},
                                  fields=["name", "customer_name", "grand_total", "outstanding_amount", "status"],
                                  order_by="posting_date desc", limit=8)
            return rows or "No invoices yet."
        if name == "get_document":
            doctype = inp.get("doctype")
            if not doctype or not frappe.has_permission(doctype, ptype="read"):
                return "You do not have permission to read that DocType."
            meta = frappe.get_meta(doctype)
            password_fields = {df.fieldname for df in meta.fields if df.fieldtype == "Password"}
            if inp.get("name"):
                doc = frappe.get_doc(doctype, inp["name"])
                if not doc.has_permission("read"):
                    return "You do not have permission to read that document."
                data = doc.as_dict()
                for fieldname in password_fields:
                    data.pop(fieldname, None)
                for key, value in list(data.items()):
                    if isinstance(value, str) and len(value) > 500:
                        data[key] = value[:500] + "..."
                return {"doctype": doctype, "name": doc.name, "data": data}
            try:
                filters = json.loads(inp.get("filters") or "[]")
            except (TypeError, ValueError):
                filters = []
            try:
                fields = json.loads(inp.get("fields") or '["name"]')
            except (TypeError, ValueError):
                fields = ["name"]
            fields = [field for field in fields if field == "name" or (
                meta.has_field(field) and field not in password_fields
            )][0:20] or ["name"]
            return frappe.get_list(
                doctype, filters=filters, fields=fields,
                limit_page_length=min(cint(inp.get("limit") or 10), 50),
            ) or "No matching documents."
    except Exception as e:
        return f"Error: {e}"
    return "Unknown tool."


def _ai_summarize_action(tool, inp):
    """Human-readable summary of a pending create action for confirmation."""
    def _items(it):
        return "; ".join(f"{x.get('qty',1)} × {x.get('item_name') or x.get('item_code')} @ {x.get('rate',0)}"
                         for x in (it or []))
    if tool == "create_customer":
        return f"Create customer “{inp.get('customer_name')}”"
    if tool == "create_supplier":
        return f"Create supplier “{inp.get('supplier_name')}”"
    if tool == "create_sales_invoice":
        return f"Sales Invoice for {inp.get('customer')} — {_items(inp.get('items'))}"
    if tool == "create_sales_order":
        return f"Sales Order for {inp.get('customer')} — {_items(inp.get('items'))}"
    if tool == "create_quotation":
        return f"Quotation for {inp.get('customer')} — {_items(inp.get('items'))}"
    if tool == "create_purchase_order":
        return f"Purchase Order to {inp.get('supplier')} — {_items(inp.get('items'))}"
    if tool == "create_purchase_invoice":
        return f"Purchase Invoice from {inp.get('supplier')} — {_items(inp.get('items'))}"
    if tool == "create_document":
        return f"Create {inp.get('doctype')} with the supplied fields"
    if tool == "update_document":
        fields = ", ".join((inp.get("data") or {}).keys())
        return f"Update {inp.get('doctype')} {inp.get('name')} ({fields})"
    if tool == "submit_document":
        return f"{cstr(inp.get('action')).title()} {inp.get('doctype')} {inp.get('name')}"
    return tool


@frappe.whitelist()
def portal_ai(message, mode="chat", history=None, company=None):
    """Two-mode Claude assistant for the client portal.

    mode="chat"  → conversational Q&A (no actions).
    mode="agent" → can look up data and propose creating documents (confirm-first).
    Returns {"type": "message"|"confirm"|"error", ...}.
    """
    try:
        if not company:
            company = _get_company()
        mode = "agent" if mode == "agent" else "chat"
        memory_doc = _get_portal_memory(company)
        client = _anthropic_client()
        provider = "anthropic" if client is not None else "gemini"

        if isinstance(history, str):
            history = json.loads(history or "[]")
        history = history or []
        durable_history = _memory_history(memory_doc, mode)
        if not durable_history:
            durable_history = [
                {"role": h["role"], "content": h["content"]} for h in history
                if h.get("role") in ("user", "assistant") and h.get("content")
            ][-_AI_MEMORY_TURNS:]
        _remember_explicit_fact(memory_doc, message)
        messages = durable_history[-_AI_CONTEXT_TURNS:]
        messages.append({"role": "user", "content": message})
        memory_rows = durable_history + [{"role": "user", "content": message}]

        def finish(payload, assistant_text):
            _save_memory_history(
                memory_doc, mode,
                memory_rows + [{"role": "assistant", "content": assistant_text}],
            )
            return payload

        ctx = _portal_financial_context(company)
        today = frappe.utils.today()
        currency = frappe.get_value("Company", company, "default_currency") or "AED"
        remembered = memory_doc.memory_notes or "No explicit user facts saved."
        shared_context = (
            f"\nRemembered user/company context:\n{remembered}\n\n"
            f"{UAE_TAX_KNOWLEDGE}\n"
        )

        if mode == "agent":
            system = (
                f"You are an operations agent for {company} inside its accounting portal "
                f"(currency {currency}, today {today}). You can look up data and create documents. "
                "You can also read, create, update, submit, cancel, or amend other permitted ERPNext "
                "documents with the generic document tools. Every write is confirmed by the user. "
                "Use the read tools to resolve exact customer/supplier names and item details before "
                "creating anything. When you have enough information, call the matching create_* tool — "
                "the user will be asked to confirm before anything is saved, so do not ask for confirmation "
                "yourself. Be concise and friendly. "
                f"Current data: {ctx}. {shared_context}"
            )
            if provider == "gemini":
                from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
                llm = _gemini_llm(with_tools=True)
                if llm is None:
                    return {"type": "error", "text": "AI is not configured in NextAI Settings."}
                lc_messages = [SystemMessage(content=system)]
                for turn in messages:
                    cls = HumanMessage if turn["role"] == "user" else AIMessage
                    lc_messages.append(cls(content=turn["content"]))
                for _ in range(8):
                    resp = llm.invoke(lc_messages)
                    tool_calls = getattr(resp, "tool_calls", None) or []
                    if not tool_calls:
                        text = _gemini_message_text(resp) or "Done."
                        return finish({"type": "message", "text": text}, text)
                    write_calls = [call for call in tool_calls if call.get("name") in _AI_WRITE_TOOLS]
                    if write_calls:
                        call = write_calls[0]
                        inp = call.get("args") or {}
                        summary = _ai_summarize_action(call.get("name"), inp)
                        pre = _gemini_message_text(resp)
                        return finish(
                            {"type": "confirm", "tool": call.get("name"), "input": inp,
                             "summary": summary, "text": pre},
                            f"Pending user confirmation: {summary}",
                        )
                    lc_messages.append(resp)
                    for call in tool_calls:
                        out = _ai_run_readonly_tool(call.get("name"), call.get("args") or {}, company)
                        lc_messages.append(ToolMessage(
                            content=json.dumps(out, default=str),
                            tool_call_id=call.get("id"),
                        ))
                text = "I wasn't able to complete that — please refine the request."
                return finish({"type": "message", "text": text}, text)

            for _ in range(8):
                resp = client.messages.create(
                    model=PORTAL_AI_MODEL, max_tokens=1500,
                    system=system, tools=_AI_TOOLS, messages=messages,
                )
                if resp.stop_reason != "tool_use":
                    text = "".join(b.text for b in resp.content if b.type == "text")
                    text = text or "Done."
                    return finish({"type": "message", "text": text}, text)

                tool_uses = [b for b in resp.content if b.type == "tool_use"]
                write_calls = [b for b in tool_uses if b.name in _AI_WRITE_TOOLS]
                if write_calls:
                    b = write_calls[0]
                    pre = "".join(x.text for x in resp.content if x.type == "text")
                    summary = _ai_summarize_action(b.name, b.input)
                    return finish(
                        {"type": "confirm", "tool": b.name, "input": b.input,
                         "summary": summary, "text": pre},
                        f"Pending user confirmation: {summary}",
                    )

                # Read-only tools: execute and continue the loop.
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for b in tool_uses:
                    out = _ai_run_readonly_tool(b.name, b.input or {}, company)
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out, default=str)})
                messages.append({"role": "user", "content": results})
            text = "I wasn't able to complete that — please refine the request."
            return finish({"type": "message", "text": text}, text)

        # Chat mode — conversational, no tools.
        system = (
            f"You are a helpful, friendly business and accounting assistant for {company} "
            f"(currency {currency}). Answer clearly and conversationally, like a knowledgeable advisor. "
            "You cannot perform actions in this mode; if the user wants to create invoices, orders, or "
            "other documents, suggest switching to Agent mode. "
            "For UAE tax questions, use the supplied tax knowledge, state assumptions, distinguish general "
            "guidance from filing advice, and point to official sources for current rules. "
            f"Current financial snapshot: {ctx}. {shared_context}"
        )
        if provider == "gemini":
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
            llm = _gemini_llm()
            if llm is None:
                return {"type": "error", "text": "AI is not configured in NextAI Settings."}
            lc_messages = [SystemMessage(content=system)]
            for turn in messages:
                cls = HumanMessage if turn["role"] == "user" else AIMessage
                lc_messages.append(cls(content=turn["content"]))
            response = llm.invoke(lc_messages)
            text = _gemini_message_text(response) or "..."
            return finish({"type": "message", "text": text}, text)
        resp = client.messages.create(
            model=PORTAL_AI_MODEL, max_tokens=1200, system=system, messages=messages,
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        text = text or "…"
        return finish({"type": "message", "text": text}, text)

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal AI")
        return {"type": "error", "text": f"AI error: {e}"}


def _ai_resolve_items(items):
    """Resolve a list of {item_name/item_code, qty, rate} into invoice rows with item_code."""
    rows = []
    for it in (items or []):
        code = it.get("item_code")
        if not code or not frappe.db.exists("Item", code):
            res = get_or_create_item(it.get("item_name") or code or "Item")
            code = res.get("item_code") if isinstance(res, dict) else code
        rows.append({"item_code": code, "item_name": it.get("item_name"),
                     "qty": flt(it.get("qty", 1)), "rate": flt(it.get("rate", 0))})
    return rows


@frappe.whitelist()
def portal_ai_execute(tool, input, company=None):
    """Execute a create_* action the user confirmed from Agent mode."""
    try:
        if tool not in _AI_WRITE_TOOLS:
            frappe.throw("Unknown action.")
        inp = json.loads(input) if isinstance(input, str) else (input or {})
        if not company:
            company = _get_company()
        today = frappe.utils.today()

        def done(result):
            text = result.get("detail") or result.get("summary") or result.get("error") or "Action completed."
            _record_agent_action(company, text)
            return result

        if tool in ("create_document", "update_document", "submit_document"):
            from next_ai.ai.actions import run_action
            doctype = inp.get("doctype")
            if not doctype or not frappe.db.exists("DocType", doctype):
                frappe.throw("Unknown DocType.")
            if tool == "create_document":
                permitted = frappe.has_permission(doctype, ptype="create")
                data = inp.get("data") or {}
                if frappe.get_meta(doctype).has_field("company") and not data.get("company"):
                    data["company"] = company
                inp["data"] = data
            else:
                doc = frappe.get_doc(doctype, inp.get("name"))
                ptype = "write" if tool == "update_document" else {
                    "cancel": "cancel", "amend": "create",
                }.get(inp.get("action"), "submit")
                permitted = doc.has_permission(ptype)
            if not permitted:
                frappe.throw(f"You do not have permission for this {doctype} action.", frappe.PermissionError)
            result = run_action(tool, inp)
            result["ok"] = bool(result.get("success"))
            result["detail"] = result.get("summary") or result.get("error")
            return done(result)

        if tool == "create_customer":
            res = create_customer(inp.get("customer_name"), mobile_no=inp.get("mobile_no"),
                                  email_id=inp.get("email_id"))
            return done({"ok": True, "doctype": "Customer", "name": (res or {}).get("name") if isinstance(res, dict) else res,
                         "detail": f"Customer “{inp.get('customer_name')}” created."})
        if tool == "create_supplier":
            res = create_supplier(inp.get("supplier_name"), mobile_no=inp.get("mobile_no"),
                                  email_id=inp.get("email_id"))
            return done({"ok": True, "doctype": "Supplier", "name": (res or {}).get("name") if isinstance(res, dict) else res,
                         "detail": f"Supplier “{inp.get('supplier_name')}” created."})

        items = _ai_resolve_items(inp.get("items"))
        if tool == "create_sales_invoice":
            res = create_sales_invoice(inp.get("customer"), today, items, company=company, due_date=inp.get("due_date"))
            return done({"ok": True, "doctype": "Sales Invoice", "name": res.get("name"),
                         "detail": f"Sales Invoice {res.get('name')} — total {res.get('grand_total')}"})
        if tool == "create_sales_order":
            res = create_sales_order(inp.get("customer"), inp.get("delivery_date") or today, items, company=company)
            return done({"ok": True, "doctype": "Sales Order", "name": res.get("name"),
                         "detail": f"Sales Order {res.get('name')} created."})
        if tool == "create_quotation":
            res = create_quotation("Customer", inp.get("customer"), today, items, valid_till=inp.get("valid_till"), company=company)
            return done({"ok": True, "doctype": "Quotation", "name": res.get("name") if isinstance(res, dict) else res,
                         "detail": "Quotation created."})
        if tool == "create_purchase_order":
            res = create_purchase_order(inp.get("supplier"), today, items, company=company, schedule_date=inp.get("schedule_date"))
            return done({"ok": True, "doctype": "Purchase Order", "name": res.get("name") if isinstance(res, dict) else res,
                         "detail": "Purchase Order created."})
        if tool == "create_purchase_invoice":
            res = create_purchase_invoice(inp.get("supplier"), today, items, company=company, bill_no=inp.get("bill_no"))
            return done({"ok": True, "doctype": "Purchase Invoice", "name": res.get("name") if isinstance(res, dict) else res,
                         "detail": "Purchase Invoice created."})
        frappe.throw("Unhandled action.")
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal AI Execute")
        try:
            _record_agent_action(company or _get_company(), f"Action failed: {e}")
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


@frappe.whitelist()
def portal_ai_chat(message, company=None):
    """Backward-compatible chat endpoint (plain text reply)."""
    res = portal_ai(message, mode="chat", company=company)
    return res.get("text") if isinstance(res, dict) else res


@frappe.whitelist()
def get_whatsapp_config():
    return {
        "enabled":      int(frappe.db.get_single_value("System Settings", "wa_chat_enabled") or 0),
        "phone":        frappe.db.get_single_value("System Settings", "wa_chat_phone") or "",
        "agent_name":   frappe.db.get_single_value("System Settings", "wa_chat_agent_name") or "Support",
        "agent_status": frappe.db.get_single_value("System Settings", "wa_chat_agent_status") or "",
        "greeting":     frappe.db.get_single_value("System Settings", "wa_chat_greeting") or "Hello! I need assistance.",
    }


@frappe.whitelist()
def get_portal_notifications(limit=20):
    limit = max(1, min(cint(limit or 20), 50))
    rows = frappe.get_all(
        "Notification Log",
        filters={"for_user": frappe.session.user},
        fields=["name", "subject", "type", "document_type", "document_name",
                "link", "read", "creation", "from_user"],
        order_by="creation desc",
        limit=limit,
    )
    for row in rows:
        row.subject = frappe.utils.strip_html(row.subject or "Notification")
    unread = frappe.db.count(
        "Notification Log", {"for_user": frappe.session.user, "read": 0}
    )
    return {"rows": rows, "unread": unread}


@frappe.whitelist(methods=["POST"])
def mark_portal_notification_read(name=None, mark_all=0):
    filters = {"for_user": frappe.session.user, "read": 0}
    if not cint(mark_all):
        if not name:
            frappe.throw("Notification name is required.")
        filters["name"] = name
    frappe.db.set_value("Notification Log", filters, "read", 1, update_modified=False)
    return {"ok": True}


@frappe.whitelist()
def get_document_attachments(doctype, name):
    doc = frappe.get_doc(doctype, name)
    if not doc.has_permission("read"):
        frappe.throw("You do not have permission to read this document.", frappe.PermissionError)
    return frappe.get_all(
        "File",
        filters={"attached_to_doctype": doctype, "attached_to_name": name, "is_folder": 0},
        fields=["name", "file_name", "file_url", "file_size", "file_type", "is_private", "creation"],
        order_by="creation desc",
        limit=100,
    )


@frappe.whitelist()
def get_portal_email_status():
    account = frappe.db.get_value(
        "Email Account", {"enable_outgoing": 1}, ["name", "email_id"], as_dict=True,
        order_by="default_outgoing desc, modified desc",
    )
    return {"configured": bool(account), "email_id": account.email_id if account else ""}


# ─────────────────────────── TEAM MEETINGS ───────────────────────────

@frappe.whitelist()
def get_team_users():
    return frappe.get_all(
        "User",
        filters={"enabled": 1, "user_type": "System User"},
        fields=["name", "full_name", "user_image"],
        order_by="full_name asc",
        limit=500,
    )


@frappe.whitelist()
def get_team_meetings(status=None, upcoming=0):
    conditions = [
        "e.event_category='Meeting'",
        "(e.owner=%(user)s OR ep.email=%(user)s OR ep.reference_docname=%(user)s)",
    ]
    params = {"user": frappe.session.user, "now": frappe.utils.now_datetime()}
    if status:
        conditions.append("e.status=%(status)s")
        params["status"] = status
    if cint(upcoming):
        conditions.append("e.ends_on >= %(now)s")
    return frappe.db.sql(f"""
        SELECT e.name, e.subject, e.starts_on, e.ends_on, e.status, e.event_type,
               e.location, e.description, e.owner, e.google_meet_link,
               GROUP_CONCAT(DISTINCT ep.email ORDER BY ep.email SEPARATOR ', ') AS participants,
               MAX(CASE WHEN ep.email=%(user)s OR ep.reference_docname=%(user)s
                        THEN ep.attending ELSE NULL END) AS my_response
        FROM `tabEvent` e
        LEFT JOIN `tabEvent Participants` ep ON ep.parent=e.name
        WHERE {' AND '.join(conditions)}
        GROUP BY e.name, e.subject, e.starts_on, e.ends_on, e.status, e.event_type,
                 e.location, e.description, e.owner, e.google_meet_link
        ORDER BY e.starts_on ASC LIMIT 300
    """, params, as_dict=True)


@frappe.whitelist(methods=["POST"])
def create_team_meeting(subject, starts_on, ends_on=None, participants=None,
                        location=None, agenda=None, event_type="Private"):
    if not frappe.has_permission("Event", ptype="create"):
        frappe.throw("You do not have permission to create meetings.", frappe.PermissionError)
    subject = cstr(subject).strip()
    if not subject:
        frappe.throw("Meeting subject is required.")
    if not starts_on:
        frappe.throw("Meeting start time is required.")
    if ends_on and frappe.utils.get_datetime(ends_on) <= frappe.utils.get_datetime(starts_on):
        frappe.throw("Meeting end time must be after the start time.")
    participants = json.loads(participants) if isinstance(participants, str) else (participants or [])
    participants = list(dict.fromkeys(cstr(user).strip() for user in participants if cstr(user).strip()))
    valid_users = set(frappe.get_all(
        "User", filters={"name": ["in", participants], "enabled": 1}, pluck="name"
    )) if participants else set()
    invalid = [user for user in participants if user not in valid_users]
    if invalid:
        frappe.throw(f"Unknown or disabled team member: {invalid[0]}")
    doc = frappe.get_doc({
        "doctype": "Event",
        "subject": subject,
        "event_category": "Meeting",
        "event_type": event_type if event_type in ("Private", "Public") else "Private",
        "starts_on": starts_on,
        "ends_on": ends_on,
        "status": "Open",
        "location": location,
        "description": agenda,
        "send_reminder": 1,
        "event_participants": [
            {"reference_doctype": "User", "reference_docname": user,
             "email": user, "attending": ""}
            for user in participants
        ],
    })
    doc.insert()
    for user in participants:
        if user == frappe.session.user:
            continue
        notification = frappe.get_doc({
            "doctype": "Notification Log",
            "for_user": user,
            "from_user": frappe.session.user,
            "type": "Alert",
            "subject": f"Meeting request: {doc.subject}",
            "document_type": "Event",
            "document_name": doc.name,
            "link": f"/app/event/{doc.name}",
            "email_content": f"Starts {doc.starts_on}. {doc.location or ''}",
        })
        notification.insert(ignore_permissions=True)
    return {"name": doc.name, "subject": doc.subject, "status": doc.status}


@frappe.whitelist(methods=["POST"])
def respond_team_meeting(name, response):
    if response not in ("Yes", "No", "Maybe"):
        frappe.throw("Response must be Yes, No, or Maybe.")
    event = frappe.get_doc("Event", name)
    if event.owner == frappe.session.user:
        event.attending = response
        event.save()
        return {"name": event.name, "response": response}
    for participant in event.event_participants:
        if participant.email == frappe.session.user or participant.reference_docname == frappe.session.user:
            frappe.db.set_value("Event Participants", participant.name, "attending", response)
            return {"name": event.name, "response": response}
    frappe.throw("You are not invited to this meeting.", frappe.PermissionError)


@frappe.whitelist(methods=["POST"])
def update_team_meeting_status(name, status):
    if status not in ("Open", "Completed", "Closed", "Cancelled"):
        frappe.throw("Invalid meeting status.")
    event = frappe.get_doc("Event", name)
    if event.owner != frappe.session.user and not event.has_permission("write"):
        frappe.throw("Only the organiser can update this meeting.", frappe.PermissionError)
    event.status = status
    event.save()
    return {"name": event.name, "status": event.status}


@frappe.whitelist(methods=["POST"])
def send_portal_document_email(doctype, name, recipients, subject, message,
                               cc=None, attachments=None, attach_print=0,
                               print_format=None):
    from frappe.core.doctype.communication.email import make
    from frappe.utils import validate_email_address
    from frappe.utils.html_utils import escape_html

    doc = frappe.get_doc(doctype, name)
    if not doc.has_permission("email"):
        frappe.throw("You do not have permission to email this document.", frappe.PermissionError)
    recipient_list = validate_email_address(recipients, throw=True) or []
    cc_list = validate_email_address(cc, throw=True) if cc else []
    if isinstance(attachments, str):
        attachments = json.loads(attachments or "[]")
    allowed_files = set(frappe.get_all(
        "File", filters={"attached_to_doctype": doctype, "attached_to_name": name}, pluck="name"
    ))
    attachments = [file_name for file_name in (attachments or []) if file_name in allowed_files]
    content = "<p>" + escape_html(cstr(message)).replace("\n", "<br>") + "</p>"
    result = make(
        doctype=doctype,
        name=name,
        subject=cstr(subject).strip(),
        content=content,
        recipients=recipient_list,
        cc=cc_list,
        send_email=True,
        attachments=attachments,
        print_format=(print_format or "Standard") if cint(attach_print) else None,
        print_letterhead=True,
    )
    return {"ok": True, "communication": result.get("name") if isinstance(result, dict) else result}


def _get_company():
    companies = frappe.get_all("Company", pluck="name", limit=1)
    return companies[0] if companies else None


def _get_naming_series(doctype):
    field = frappe.get_meta(doctype).get_field("naming_series")
    options = [line.strip() for line in (field.options or "").splitlines() if line.strip()]
    default = frappe.new_doc(doctype).get("naming_series")
    return {"options": options, "default": default or (options[0] if options else "")}


def _set_naming_series(doc, naming_series):
    if not naming_series:
        return
    available = _get_naming_series(doc.doctype)["options"]
    if naming_series not in available:
        frappe.throw(f"Invalid naming series for {doc.doctype}.")
    doc.naming_series = naming_series


@frappe.whitelist()
def get_transaction_naming_series():
    return {
        doctype: _get_naming_series(doctype)
        for doctype in ("Sales Invoice", "Sales Order", "Delivery Note")
    }


# ── Invoice helpers (GL account / VAT / discount) ────────────────
def _default_tax_account(company):
    acc = frappe.get_all("Account", filters={"company": company, "account_type": "Tax", "is_group": 0},
                         pluck="name", limit=1)
    return acc[0] if acc else None


def _inv_item_rows(items, account_field):
    rows = []
    for i in items:
        row = {
            "item_code": cstr(i["item_code"]),
            "qty": flt(i.get("qty", 1)),
            "rate": flt(i.get("rate", 0)),
            "uom": cstr(i.get("uom") or "Nos"),
            "conversion_factor": 1,
        }
        if i.get("item_name"):
            row["item_name"] = cstr(i["item_name"])
        if flt(i.get("discount_percentage")):
            row["discount_percentage"] = flt(i.get("discount_percentage"))
        if i.get("account"):
            row[account_field] = cstr(i["account"])
        rows.append(row)
    return rows


def _apply_inv_tax(doc, template_doctype, taxes_and_charges, tax_rate, tax_account, company, label="VAT"):
    """Attach VAT via a tax template, or a single 'On Net Total' rate line."""
    if taxes_and_charges:
        tpl = frappe.get_doc(template_doctype, taxes_and_charges)
        doc.taxes_and_charges = taxes_and_charges
        for r in tpl.taxes:
            d = {k: r.get(k) for k in ("charge_type", "account_head", "rate", "description",
                                       "included_in_print_rate", "cost_center", "row_id",
                                       "add_deduct_tax", "category") if r.get(k) is not None}
            doc.append("taxes", d)
    elif flt(tax_rate):
        head = tax_account or _default_tax_account(company)
        if not head:
            frappe.throw("No tax account found. Create a VAT account or choose a tax template.")
        doc.append("taxes", {"charge_type": "On Net Total", "account_head": head,
                             "rate": flt(tax_rate), "description": f"{label} {flt(tax_rate)}%"})


def _apply_inv_discount(doc, additional_discount_percentage, apply_discount_on):
    if flt(additional_discount_percentage):
        doc.apply_discount_on = apply_discount_on or "Grand Total"
        doc.additional_discount_percentage = flt(additional_discount_percentage)


# ── Sales Invoice ────────────────────────────────────────────────
@frappe.whitelist()
def create_sales_invoice(customer, posting_date, items, company=None, due_date=None,
                         remarks=None, currency=None, conversion_rate=None, naming_series=None,
                         taxes_and_charges=None, tax_rate=None, tax_account=None,
                         additional_discount_percentage=None, apply_discount_on="Grand Total"):
    try:
        if isinstance(items, str):
            items = json.loads(items)

        if not company:
            company = _get_company()
        if not company:
            frappe.throw("No company found. Please set up a Company first.")

        rows = _inv_item_rows(items, "income_account")
        if not rows:
            frappe.throw("Please add at least one item.")

        company_currency = frappe.get_value("Company", company, "default_currency") or "AED"
        inv_currency = cstr(currency) if currency else company_currency

        doc = frappe.get_doc({
            "doctype": "Sales Invoice",
            "customer": customer,
            "posting_date": posting_date or nowdate(),
            "company": company,
            "currency": inv_currency,
            "items": rows,
        })
        if inv_currency != company_currency and conversion_rate:
            doc.conversion_rate = flt(conversion_rate)
        if due_date:
            doc.due_date = due_date
        if remarks:
            doc.remarks = remarks
        _apply_inv_tax(doc, "Sales Taxes and Charges Template", taxes_and_charges, tax_rate, tax_account, company)
        _apply_inv_discount(doc, additional_discount_percentage, apply_discount_on)
        _set_naming_series(doc, naming_series)

        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()

        return {"name": doc.name, "grand_total": doc.grand_total, "status": doc.status}

    except frappe.ValidationError as e:
        frappe.throw(str(e))
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Sales Invoice")
        frappe.throw(str(e))


# ── Purchase Invoice ─────────────────────────────────────────────
@frappe.whitelist()
def create_purchase_invoice(supplier, posting_date, items, company=None, bill_no=None, remarks=None,
                            due_date=None, taxes_and_charges=None, tax_rate=None, tax_account=None,
                            additional_discount_percentage=None, apply_discount_on="Grand Total"):
    try:
        if isinstance(items, str):
            items = json.loads(items)

        if not company:
            company = _get_company()
        if not company:
            frappe.throw("No company found.")

        rows = _inv_item_rows(items, "expense_account")
        if not rows:
            frappe.throw("Please add at least one item.")

        doc = frappe.get_doc({
            "doctype": "Purchase Invoice",
            "supplier": supplier,
            "posting_date": posting_date or nowdate(),
            "company": company,
            "items": rows,
        })
        if bill_no:
            doc.bill_no = bill_no
            doc.bill_date = posting_date or nowdate()
        if due_date:
            doc.due_date = due_date
        if remarks:
            doc.remarks = remarks
        _apply_inv_tax(doc, "Purchase Taxes and Charges Template", taxes_and_charges, tax_rate, tax_account, company)
        _apply_inv_discount(doc, additional_discount_percentage, apply_discount_on)

        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()

        return {"name": doc.name, "grand_total": doc.grand_total, "status": doc.status}

    except frappe.ValidationError as e:
        frappe.throw(str(e))
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Purchase Invoice")
        frappe.throw(str(e))


# ── Transaction metadata for entry forms (GL accounts + VAT templates) ──
@frappe.whitelist()
def get_transaction_meta(party_type="sales", company=None):
    """Return GL accounts and VAT templates for invoice entry dropdowns."""
    try:
        if not company:
            company = _get_company()
        currency = frappe.get_value("Company", company, "default_currency") or "AED"
        if party_type == "purchase":
            root = "Expense"
            tpl_doctype = "Purchase Taxes and Charges Template"
        else:
            root = "Income"
            tpl_doctype = "Sales Taxes and Charges Template"
        accounts = frappe.get_all("Account",
                                  filters={"company": company, "is_group": 0, "root_type": root},
                                  fields=["name", "account_name"], order_by="name")
        templates = frappe.get_all(tpl_doctype, filters={"company": company},
                                   fields=["name"], order_by="name")
        return {"accounts": accounts, "templates": [t.name for t in templates], "currency": currency}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_transaction_meta")
        return {"accounts": [], "templates": [], "currency": "AED"}


# ── Customer ─────────────────────────────────────────────────────
@frappe.whitelist()
def create_customer(customer_name, customer_type="Company", customer_group=None,
                    territory=None, mobile_no=None, email_id=None, tax_id=None):
    try:
        if not customer_group:
            grp = frappe.get_all("Customer Group", pluck="name", limit=1)
            customer_group = grp[0] if grp else "All Customer Groups"

        doc = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": customer_name,
            "customer_type": customer_type,
            "customer_group": customer_group,
        })
        if territory:
            doc.territory = territory
        if mobile_no:
            doc.mobile_no = mobile_no
        if email_id:
            doc.email_id = email_id
        if tax_id:
            doc.tax_id = tax_id

        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "customer_name": doc.customer_name}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Customer")
        frappe.throw(str(e))


@frappe.whitelist()
def get_customer_connections(customer):
    """Customer master details plus linked contacts, addresses, and transactions."""
    doc = frappe.get_doc("Customer", customer)

    contacts = frappe.db.sql("""
        SELECT c.name, c.first_name, c.last_name, c.email_id, c.mobile_no,
               c.is_primary_contact
        FROM `tabContact` c
        INNER JOIN `tabDynamic Link` dl ON dl.parent=c.name
        WHERE dl.parenttype='Contact' AND dl.link_doctype='Customer'
          AND dl.link_name=%(customer)s
        ORDER BY c.is_primary_contact DESC, c.modified DESC
    """, {"customer": customer}, as_dict=True)

    addresses = frappe.db.sql("""
        SELECT a.name, a.address_title, a.address_type, a.address_line1,
               a.address_line2, a.city, a.state, a.pincode, a.country,
               a.is_primary_address, a.is_shipping_address
        FROM `tabAddress` a
        INNER JOIN `tabDynamic Link` dl ON dl.parent=a.name
        WHERE dl.parenttype='Address' AND dl.link_doctype='Customer'
          AND dl.link_name=%(customer)s
        ORDER BY a.is_primary_address DESC, a.modified DESC
    """, {"customer": customer}, as_dict=True)

    specs = [
        ("Quotation", {"quotation_to": "Customer", "party_name": customer}, "transaction_date", "grand_total"),
        ("Sales Order", {"customer": customer}, "transaction_date", "grand_total"),
        ("Delivery Note", {"customer": customer}, "posting_date", "grand_total"),
        ("Sales Invoice", {"customer": customer}, "posting_date", "grand_total"),
        ("Payment Entry", {"party_type": "Customer", "party": customer}, "posting_date", "paid_amount"),
    ]
    connections = []
    for doctype, filters, date_field, amount_field in specs:
        rows = frappe.get_all(
            doctype,
            filters=filters,
            fields=["name", date_field, amount_field, "docstatus"],
            order_by=f"{date_field} desc",
            limit=8,
            ignore_permissions=True,
        )
        connections.append({
            "doctype": doctype,
            "count": frappe.db.count(doctype, filters),
            "rows": [
                {
                    "name": row.name,
                    "date": row.get(date_field),
                    "amount": flt(row.get(amount_field)),
                    "docstatus": row.docstatus,
                }
                for row in rows
            ],
        })

    return {
        "customer": {
            "name": doc.name,
            "customer_name": doc.customer_name,
            "customer_type": doc.customer_type,
            "tax_id": doc.tax_id,
            "tax_category": doc.tax_category,
            "mobile_no": doc.mobile_no,
            "email_id": doc.email_id,
            "territory": doc.territory,
            "customer_group": doc.customer_group,
        },
        "contacts": contacts,
        "addresses": addresses,
        "connections": connections,
    }


# ── Supplier ─────────────────────────────────────────────────────
@frappe.whitelist()
def create_supplier(supplier_name, supplier_type="Company", supplier_group=None,
                    country=None, mobile_no=None, email_id=None, tax_id=None):
    try:
        if not supplier_group:
            grp = frappe.get_all("Supplier Group", pluck="name", limit=1)
            supplier_group = grp[0] if grp else "All Supplier Groups"

        doc = frappe.get_doc({
            "doctype": "Supplier",
            "supplier_name": supplier_name,
            "supplier_type": supplier_type,
            "supplier_group": supplier_group,
        })
        if country:
            doc.country = country
        if mobile_no:
            doc.mobile_no = mobile_no
        if email_id:
            doc.email_id = email_id
        if tax_id:
            doc.tax_id = tax_id

        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "supplier_name": doc.supplier_name}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Supplier")
        frappe.throw(str(e))


# ── Payment Entry ─────────────────────────────────────────────────
@frappe.whitelist()
def create_payment_entry(payment_type, party_type, party, posting_date,
                         paid_amount, paid_from, paid_to, mode_of_payment="Cash",
                         company=None, reference_no=None, remarks=None):
    try:
        if not company:
            company = _get_company()
        if not company:
            frappe.throw("No company found.")

        paid_amount = flt(paid_amount)
        if paid_amount <= 0:
            frappe.throw("Amount must be greater than zero.")

        currency = frappe.get_value("Company", company, "default_currency") or "AED"

        doc = frappe.get_doc({
            "doctype": "Payment Entry",
            "payment_type": payment_type,
            "party_type": party_type,
            "party": party,
            "posting_date": posting_date or nowdate(),
            "paid_amount": paid_amount,
            "received_amount": paid_amount,
            "paid_from": paid_from,
            "paid_to": paid_to,
            "paid_from_account_currency": currency,
            "paid_to_account_currency": currency,
            "source_exchange_rate": 1,
            "target_exchange_rate": 1,
            "mode_of_payment": mode_of_payment,
            "company": company,
        })
        if reference_no:
            doc.reference_no = reference_no
            doc.reference_date = posting_date or nowdate()
        if remarks:
            doc.remarks = remarks

        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()

        return {"name": doc.name, "status": "Submitted"}

    except frappe.ValidationError as e:
        frappe.throw(str(e))
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Payment Entry")
        frappe.throw(str(e))


# ── Sales Order ──────────────────────────────────────────────────
@frappe.whitelist()
def create_sales_order(customer, delivery_date, items, company=None, remarks=None, naming_series=None):
    try:
        if isinstance(items, str):
            items = json.loads(items)
        if not company:
            company = _get_company()
        if not company:
            frappe.throw("No company found.")

        rows = []
        for i in items:
            row = {"item_code": cstr(i["item_code"]), "qty": flt(i.get("qty", 1)),
                   "rate": flt(i.get("rate", 0)), "uom": cstr(i.get("uom") or "Nos"),
                   "conversion_factor": 1, "delivery_date": delivery_date}
            if i.get("item_name"):
                row["item_name"] = cstr(i["item_name"])
            rows.append(row)

        if not rows:
            frappe.throw("Please add at least one item.")

        doc = frappe.get_doc({"doctype": "Sales Order", "customer": customer,
            "delivery_date": delivery_date or nowdate(), "company": company, "items": rows})
        if remarks:
            doc.remarks = remarks
        _set_naming_series(doc, naming_series)
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return {"name": doc.name, "grand_total": doc.grand_total, "status": doc.status}
    except frappe.ValidationError as e:
        frappe.throw(str(e))
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Sales Order")
        frappe.throw(str(e))


@frappe.whitelist()
def get_sales_order_workflow_info(sales_order):
    fixed_asset_items = frappe.db.sql("""
        SELECT soi.name AS so_detail, soi.item_code, soi.item_name
        FROM `tabSales Order Item` soi
        INNER JOIN `tabItem` i ON i.name=soi.item_code
        WHERE soi.parent=%(sales_order)s AND i.is_fixed_asset=1
    """, {"sales_order": sales_order}, as_dict=True)
    for item in fixed_asset_items:
        item["assets"] = frappe.get_all(
            "Asset",
            filters={"item_code": item.item_code, "docstatus": 1, "status": ["not in", ["Sold", "Scrapped"]]},
            fields=["name", "asset_name", "status"],
            order_by="asset_name",
        )
    return {"fixed_asset_items": fixed_asset_items}


@frappe.whitelist()
def create_sales_order_followup(sales_order, action, amount=None, posting_date=None,
                                reference_no=None, asset_assignments=None):
    """Create a linked draft transaction from a submitted Sales Order."""
    so = frappe.get_doc("Sales Order", sales_order)
    if so.docstatus != 1:
        frappe.throw("Sales Order must be submitted before creating the next document.")

    if action == "delivery_note":
        from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

        target = make_delivery_note(so.name)
        target.posting_date = posting_date or nowdate()
    elif action == "sales_invoice":
        from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

        target = make_sales_invoice(so.name, ignore_permissions=True)
        target.posting_date = posting_date or nowdate()
        assignments = json.loads(asset_assignments) if isinstance(asset_assignments, str) else (asset_assignments or {})
        for item in target.items:
            if item.so_detail and assignments.get(item.so_detail):
                item.asset = assignments[item.so_detail]
    elif action == "advance_payment":
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        target = get_payment_entry(
            "Sales Order",
            so.name,
            party_amount=flt(amount) if amount else None,
            reference_date=posting_date or nowdate(),
            ignore_permissions=True,
        )
        target.posting_date = posting_date or nowdate()
        target.reference_no = reference_no or f"ADV-{so.name}-{posting_date or nowdate()}"
        target.reference_date = posting_date or nowdate()
        if amount:
            target.paid_amount = flt(amount)
            target.received_amount = flt(amount)
    else:
        frappe.throw("Unsupported Sales Order action.")

    target.flags.ignore_permissions = True
    target.insert(ignore_permissions=True)
    return {
        "name": target.name,
        "doctype": target.doctype,
        "status": "Draft",
        "grand_total": flt(target.get("grand_total") or target.get("paid_amount")),
    }


@frappe.whitelist()
def create_delivery_note_invoice(delivery_note, posting_date=None):
    from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

    source = frappe.get_doc("Delivery Note", delivery_note)
    if source.docstatus != 1:
        frappe.throw("Delivery Note must be submitted before creating an invoice.")
    target = make_sales_invoice(source.name)
    target.posting_date = posting_date or nowdate()
    target.flags.ignore_permissions = True
    target.insert(ignore_permissions=True)
    return {"name": target.name, "doctype": target.doctype, "status": "Draft", "grand_total": flt(target.grand_total)}


@frappe.whitelist()
def create_sales_invoice_payment(sales_invoice, amount, posting_date=None, reference_no=None):
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    source = frappe.get_doc("Sales Invoice", sales_invoice)
    if source.docstatus != 1 or flt(source.outstanding_amount) <= 0:
        frappe.throw("Sales Invoice must be submitted with an outstanding amount.")
    amount = flt(amount)
    if amount <= 0 or amount > flt(source.outstanding_amount):
        frappe.throw("Payment must be greater than zero and cannot exceed the outstanding amount.")
    target = get_payment_entry(
        "Sales Invoice", source.name, party_amount=amount,
        reference_date=posting_date or nowdate(), ignore_permissions=True,
    )
    target.posting_date = posting_date or nowdate()
    target.paid_amount = amount
    target.received_amount = amount
    target.reference_no = reference_no or f"PAY-{source.name}-{posting_date or nowdate()}"
    target.reference_date = posting_date or nowdate()
    target.flags.ignore_permissions = True
    target.insert(ignore_permissions=True)
    return {"name": target.name, "doctype": target.doctype, "status": "Draft", "grand_total": amount}


# ── Delivery Note ─────────────────────────────────────────────────
@frappe.whitelist()
def create_delivery_note(customer, posting_date, items, company=None, remarks=None, naming_series=None):
    try:
        if isinstance(items, str):
            items = json.loads(items)
        if not company:
            company = _get_company()
        if not company:
            frappe.throw("No company found.")

        rows = []
        for i in items:
            row = {"item_code": cstr(i["item_code"]), "qty": flt(i.get("qty", 1)),
                   "uom": cstr(i.get("uom") or "Nos"), "conversion_factor": 1}
            if i.get("item_name"):
                row["item_name"] = cstr(i["item_name"])
            if i.get("rate"):
                row["rate"] = flt(i["rate"])
            rows.append(row)

        if not rows:
            frappe.throw("Please add at least one item.")

        doc = frappe.get_doc({"doctype": "Delivery Note", "customer": customer,
            "posting_date": posting_date or nowdate(), "company": company, "items": rows})
        if remarks:
            doc.remarks = remarks
        _set_naming_series(doc, naming_series)
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return {"name": doc.name, "status": doc.status}
    except frappe.ValidationError as e:
        frappe.throw(str(e))
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Delivery Note")
        frappe.throw(str(e))


# ── Update Customer ──────────────────────────────────────────────
@frappe.whitelist()
def update_customer(customer_name, customer_type=None, mobile_no=None,
                    email_id=None, tax_id=None, territory=None):
    try:
        doc = frappe.get_doc("Customer", customer_name)
        if customer_type:
            doc.customer_type = customer_type
        if mobile_no is not None:
            doc.mobile_no = mobile_no
        if email_id is not None:
            doc.email_id = email_id
        if tax_id is not None:
            doc.tax_id = tax_id
        if territory:
            doc.territory = territory
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        return {"name": doc.name, "customer_name": doc.customer_name}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Update Customer")
        frappe.throw(str(e))


# ── Get or Create Item ────────────────────────────────────────────
@frappe.whitelist()
def get_or_create_item(item_name, item_code=None, uom="Nos"):
    """Return existing item_code or create a new item and return its code."""
    try:
        # Check by explicit item_code first
        if item_code and frappe.db.exists("Item", item_code):
            return {"item_code": item_code, "created": False}

        # Check by item_name
        existing = frappe.db.get_value("Item", {"item_name": item_name}, "name")
        if existing:
            return {"item_code": existing, "created": False}

        # Create new item
        grp = frappe.get_all("Item Group", pluck="name", limit=1)
        item_group = grp[0] if grp else "All Item Groups"
        code = (item_code or item_name)[:140]

        doc = frappe.get_doc({
            "doctype": "Item",
            "item_code": code,
            "item_name": item_name,
            "item_group": item_group,
            "stock_uom": uom or "Nos",
            "is_sales_item": 1,
            "is_purchase_item": 1,
            "is_stock_item": 0,
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"item_code": doc.name, "created": True}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Get or Create Item")
        frappe.throw(str(e))


@frappe.whitelist()
def create_purchase_order(supplier, transaction_date, items, company=None, schedule_date=None, remarks=None):
    try:
        if not company:
            company = _get_company()
        items = json.loads(items) if isinstance(items, str) else items
        doc = frappe.get_doc({
            "doctype": "Purchase Order",
            "supplier": supplier,
            "transaction_date": transaction_date,
            "schedule_date": schedule_date or transaction_date,
            "company": company,
            "remarks": remarks,
            "items": [{"item_code": i["item_code"], "qty": flt(i.get("qty", 1)), "rate": flt(i.get("rate", 0))} for i in items],
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "grand_total": doc.grand_total}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create PO")
        frappe.throw(str(e))


@frappe.whitelist()
def create_asset(asset_name, asset_category, purchase_date, gross_purchase_amount, company=None,
                 available_for_use_date=None, location=None, custodian=None):
    try:
        if not company:
            company = _get_company()
        doc = frappe.get_doc({
            "doctype": "Asset",
            "asset_name": asset_name,
            "asset_category": asset_category,
            "company": company,
            "purchase_date": purchase_date,
            "available_for_use_date": available_for_use_date or purchase_date,
            "gross_purchase_amount": flt(gross_purchase_amount),
            "location": location,
            "custodian": custodian,
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "asset_name": doc.asset_name}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Asset")
        frappe.throw(str(e))


@frappe.whitelist()
def create_journal_entry(voucher_type, posting_date, accounts, company=None, user_remark=None):
    try:
        if not company:
            company = _get_company()
        accounts = json.loads(accounts) if isinstance(accounts, str) else accounts
        doc = frappe.get_doc({
            "doctype": "Journal Entry",
            "voucher_type": voucher_type,
            "posting_date": posting_date,
            "company": company,
            "user_remark": user_remark,
            "accounts": [
                {
                    "account": a["account"],
                    "debit_in_account_currency": flt(a.get("debit_in_account_currency", 0)),
                    "credit_in_account_currency": flt(a.get("credit_in_account_currency", 0)),
                }
                for a in accounts if a.get("account")
            ],
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create JE")
        frappe.throw(str(e))


@frappe.whitelist()
def create_project(project_name, company=None, status="Open", priority="Medium",
                   expected_start_date=None, expected_end_date=None, customer=None,
                   department=None, notes=None):
    try:
        if not company:
            company = _get_company()
        doc = frappe.get_doc({
            "doctype": "Project",
            "project_name": project_name,
            "company": company,
            "status": status,
            "priority": priority,
            "expected_start_date": expected_start_date,
            "expected_end_date": expected_end_date,
            "customer": customer or None,
            "department": department or None,
            "notes": notes,
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "project_name": doc.project_name}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Project")
        frappe.throw(str(e))


@frappe.whitelist()
def create_task(subject, project=None, status="Open", priority="Medium",
                exp_end_date=None, description=None, assigned_to=None, company=None):
    try:
        company = company or _get_company()
        doc = frappe.get_doc({
            "doctype": "Task",
            "subject": subject,
            "project": project or None,
            "status": status,
            "priority": priority,
            "company": company,
            "exp_end_date": exp_end_date or None,
            "description": description,
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        if assigned_to:
            try:
                frappe.get_doc({
                    "doctype": "ToDo",
                    "reference_type": "Task",
                    "reference_name": doc.name,
                    "owner": assigned_to,
                    "description": subject,
                }).insert(ignore_permissions=True)
            except Exception:
                pass
        return {"name": doc.name, "subject": doc.subject}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Task")
        frappe.throw(str(e))


@frappe.whitelist()
def get_project_dashboard(company=None):
    company = company or _get_company()
    params = {"company": company, "today": nowdate()}
    projects = frappe.db.sql("""
        SELECT p.name, p.project_name, p.status, p.priority, p.customer,
               p.expected_start_date, p.expected_end_date, p.percent_complete,
               p.estimated_costing, p.total_costing_amount,
               COUNT(t.name) AS task_count,
               SUM(CASE WHEN t.status='Completed' THEN 1 ELSE 0 END) AS completed_tasks,
               SUM(CASE WHEN t.status NOT IN ('Completed','Cancelled')
                         AND t.exp_end_date < %(today)s THEN 1 ELSE 0 END) AS overdue_tasks
        FROM `tabProject` p
        LEFT JOIN `tabTask` t ON t.project=p.name AND t.is_group=0
        WHERE p.company=%(company)s
        GROUP BY p.name, p.project_name, p.status, p.priority, p.customer,
                 p.expected_start_date, p.expected_end_date, p.percent_complete,
                 p.estimated_costing, p.total_costing_amount
        ORDER BY FIELD(p.status,'Open','Completed','Cancelled'),
                 p.expected_end_date IS NULL, p.expected_end_date
        LIMIT 200
    """, params, as_dict=True)
    recent_tasks = frappe.db.sql("""
        SELECT t.name, t.subject, t.project, p.project_name, t.status, t.priority,
               t.exp_start_date, t.exp_end_date, t.progress,
               GROUP_CONCAT(DISTINCT td.owner ORDER BY td.owner SEPARATOR ', ') AS assigned_to
        FROM `tabTask` t
        LEFT JOIN `tabProject` p ON p.name=t.project
        LEFT JOIN `tabToDo` td ON td.reference_type='Task' AND td.reference_name=t.name
                                  AND td.status='Open'
        WHERE (p.company=%(company)s OR (t.project IS NULL AND t.company=%(company)s))
          AND t.is_group=0
        GROUP BY t.name, t.subject, t.project, p.project_name, t.status, t.priority,
                 t.exp_start_date, t.exp_end_date, t.progress
        ORDER BY t.modified DESC LIMIT 12
    """, params, as_dict=True)
    open_projects = [p for p in projects if p.status == "Open"]
    return {
        "summary": {
            "total_projects": len(projects),
            "open_projects": len(open_projects),
            "completed_projects": sum(1 for p in projects if p.status == "Completed"),
            "overdue_tasks": sum(cint(p.overdue_tasks) for p in projects),
            "avg_progress": round(
                sum(flt(p.percent_complete) for p in open_projects) / len(open_projects), 1
            ) if open_projects else 0,
        },
        "projects": projects,
        "recent_tasks": recent_tasks,
    }


@frappe.whitelist()
def get_projects(company=None, search=None, status=None):
    company = company or _get_company()
    conditions = ["p.company=%(company)s"]
    params = {"company": company, "today": nowdate()}
    if search:
        conditions.append("(p.name LIKE %(search)s OR p.project_name LIKE %(search)s)")
        params["search"] = f"%{search}%"
    if status:
        conditions.append("p.status=%(status)s")
        params["status"] = status
    return frappe.db.sql(f"""
        SELECT p.name, p.project_name, p.status, p.priority, p.customer, p.department,
               p.expected_start_date, p.expected_end_date, p.percent_complete,
               p.estimated_costing, p.total_costing_amount, p.total_billed_amount,
               COUNT(t.name) AS task_count,
               SUM(CASE WHEN t.status='Completed' THEN 1 ELSE 0 END) AS completed_tasks,
               SUM(CASE WHEN t.status NOT IN ('Completed','Cancelled')
                         AND t.exp_end_date < %(today)s THEN 1 ELSE 0 END) AS overdue_tasks
        FROM `tabProject` p
        LEFT JOIN `tabTask` t ON t.project=p.name AND t.is_group=0
        WHERE {' AND '.join(conditions)}
        GROUP BY p.name, p.project_name, p.status, p.priority, p.customer, p.department,
                 p.expected_start_date, p.expected_end_date, p.percent_complete,
                 p.estimated_costing, p.total_costing_amount, p.total_billed_amount
        ORDER BY p.modified DESC LIMIT 500
    """, params, as_dict=True)


@frappe.whitelist()
def get_project_tasks(project=None, status=None, search=None, company=None):
    company = company or _get_company()
    conditions = ["t.is_group=0", "(p.company=%(company)s OR (t.project IS NULL AND t.company=%(company)s))"]
    params = {"company": company}
    if project:
        conditions.append("t.project=%(project)s")
        params["project"] = project
    if status:
        conditions.append("t.status=%(status)s")
        params["status"] = status
    if search:
        conditions.append("(t.name LIKE %(search)s OR t.subject LIKE %(search)s)")
        params["search"] = f"%{search}%"
    return frappe.db.sql(f"""
        SELECT t.name, t.subject, t.project, p.project_name, t.status, t.priority,
               t.exp_start_date, t.exp_end_date, t.progress, t.expected_time,
               t.is_milestone, t.description,
               GROUP_CONCAT(DISTINCT td.owner ORDER BY td.owner SEPARATOR ', ') AS assigned_to
        FROM `tabTask` t
        LEFT JOIN `tabProject` p ON p.name=t.project
        LEFT JOIN `tabToDo` td ON td.reference_type='Task' AND td.reference_name=t.name
                                  AND td.status='Open'
        WHERE {' AND '.join(conditions)}
        GROUP BY t.name, t.subject, t.project, p.project_name, t.status, t.priority,
                 t.exp_start_date, t.exp_end_date, t.progress, t.expected_time,
                 t.is_milestone, t.description
        ORDER BY FIELD(t.status,'Overdue','Working','Open','Pending Review','Completed','Cancelled'),
                 t.exp_end_date IS NULL, t.exp_end_date
        LIMIT 1000
    """, params, as_dict=True)


@frappe.whitelist(methods=["POST"])
def update_project_status(name, status):
    if status not in ("Open", "Completed", "Cancelled"):
        frappe.throw("Invalid project status.")
    doc = frappe.get_doc("Project", name)
    doc.status = status
    doc.save(ignore_permissions=True)
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
def update_task_progress(name, status, progress=0):
    allowed = ("Open", "Working", "Pending Review", "Overdue", "Completed", "Cancelled")
    if status not in allowed:
        frappe.throw("Invalid task status.")
    doc = frappe.get_doc("Task", name)
    doc.status = status
    doc.progress = 100 if status == "Completed" else max(0, min(100, flt(progress)))
    doc.save(ignore_permissions=True)
    return {"name": doc.name, "status": doc.status, "progress": doc.progress}


@frappe.whitelist()
def assign_task(name, assign_to):
    """Assign a Task to one or more users (comma-separated or JSON list).
    Replaces any existing assignment with the provided set."""
    from frappe.desk.form.assign_to import add as _assign_add, remove as _assign_remove

    if isinstance(assign_to, str):
        assign_to = assign_to.strip()
        if assign_to.startswith("["):
            users = json.loads(assign_to)
        else:
            users = [u.strip() for u in assign_to.split(",") if u.strip()]
    else:
        users = assign_to or []
    if not users:
        frappe.throw("Select at least one user to assign.")

    if not frappe.db.exists("Task", name):
        frappe.throw("Task not found.")

    # Clear existing assignments, then add the new set.
    existing = frappe.get_all(
        "ToDo",
        filters={"reference_type": "Task", "reference_name": name, "status": ("!=", "Cancelled")},
        pluck="allocated_to",
    )
    for u in existing:
        try:
            _assign_remove("Task", name, u)
        except Exception:
            pass

    _assign_add({"doctype": "Task", "name": name, "assign_to": users})
    frappe.db.commit()
    return {"name": name, "assigned_to": ", ".join(users)}


@frappe.whitelist()
def portal_search(query=None, limit=6):
    """Global search across the main portal doctypes. Returns grouped results
    each carrying the portal `nav` section to jump to."""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    like = f"%{q}%"
    limit = cint(limit) or 6
    company = _get_company()
    out = []

    def add(label, icon, color, nav, rows, fmt):
        if rows:
            out.append({"group": label, "icon": icon, "color": color,
                        "nav": nav, "items": [fmt(r) for r in rows]})

    def run(sql, params=None):
        try:
            return frappe.db.sql(sql, params or {}, as_dict=True)
        except Exception:
            return []

    add("Customers", "fa-users", "#0d9488", "customers",
        run("SELECT name, customer_name FROM `tabCustomer` WHERE disabled=0 AND (name LIKE %(q)s OR customer_name LIKE %(q)s) LIMIT %(l)s", {"q": like, "l": limit}),
        lambda r: {"id": r.name, "title": r.customer_name or r.name, "sub": r.name})

    add("Suppliers", "fa-industry", "#ea580c", "suppliers",
        run("SELECT name, supplier_name FROM `tabSupplier` WHERE disabled=0 AND (name LIKE %(q)s OR supplier_name LIKE %(q)s) LIMIT %(l)s", {"q": like, "l": limit}),
        lambda r: {"id": r.name, "title": r.supplier_name or r.name, "sub": r.name})

    add("Sales Invoices", "fa-file-invoice-dollar", "#059669", "sales-invoices",
        run("SELECT name, customer_name, grand_total FROM `tabSales Invoice` WHERE docstatus<2 AND company=%(co)s AND (name LIKE %(q)s OR customer_name LIKE %(q)s) ORDER BY posting_date DESC LIMIT %(l)s", {"q": like, "co": company, "l": limit}),
        lambda r: {"id": r.name, "title": r.name, "sub": r.customer_name or ""})

    add("Purchase Invoices", "fa-file-invoice", "#dc2626", "purchase-invoices",
        run("SELECT name, supplier_name, bill_no FROM `tabPurchase Invoice` WHERE docstatus<2 AND company=%(co)s AND (name LIKE %(q)s OR supplier_name LIKE %(q)s OR bill_no LIKE %(q)s) ORDER BY posting_date DESC LIMIT %(l)s", {"q": like, "co": company, "l": limit}),
        lambda r: {"id": r.name, "title": r.bill_no or r.name, "sub": r.supplier_name or r.name})

    add("Items", "fa-box", "#2563eb", "stock-items",
        run("SELECT name, item_name FROM `tabItem` WHERE disabled=0 AND (name LIKE %(q)s OR item_name LIKE %(q)s) LIMIT %(l)s", {"q": like, "l": limit}),
        lambda r: {"id": r.name, "title": r.item_name or r.name, "sub": r.name})

    add("Tasks", "fa-list-check", "#d97706", "tasks",
        run("SELECT name, subject FROM `tabTask` WHERE (name LIKE %(q)s OR subject LIKE %(q)s) ORDER BY modified DESC LIMIT %(l)s", {"q": like, "l": limit}),
        lambda r: {"id": r.name, "title": r.subject or r.name, "sub": r.name})

    add("Projects", "fa-diagram-project", "#2563eb", "projects",
        run("SELECT name, project_name FROM `tabProject` WHERE (name LIKE %(q)s OR project_name LIKE %(q)s) ORDER BY modified DESC LIMIT %(l)s", {"q": like, "l": limit}),
        lambda r: {"id": r.name, "title": r.project_name or r.name, "sub": r.name})

    add("Employees", "fa-id-badge", "#0f766e", "employees",
        run("SELECT name, employee_name FROM `tabEmployee` WHERE status='Active' AND (name LIKE %(q)s OR employee_name LIKE %(q)s) LIMIT %(l)s", {"q": like, "l": limit}),
        lambda r: {"id": r.name, "title": r.employee_name or r.name, "sub": r.name})

    add("Leads", "fa-bullseye", "#e11d48", "crm-leads",
        run("SELECT name, lead_name, company_name FROM `tabLead` WHERE (name LIKE %(q)s OR lead_name LIKE %(q)s OR company_name LIKE %(q)s) ORDER BY modified DESC LIMIT %(l)s", {"q": like, "l": limit}),
        lambda r: {"id": r.name, "title": r.lead_name or r.company_name or r.name, "sub": r.name})

    return out


@frappe.whitelist()
def get_portal_metadata():
    """Return lists needed for Quick Create dropdowns."""
    def safe_list(dt, fields, filters=None):
        try:
            return frappe.get_all(dt, fields=fields, filters=filters or {}, limit=300)
        except Exception:
            return []

    return {
        "companies": safe_list("Company", ["name"]),
        "customers": safe_list("Customer", ["name", "customer_name"], {"disabled": 0}),
        "suppliers": safe_list("Supplier", ["name"], {"disabled": 0}),
        "departments": safe_list("Department", ["name"]),
        "asset_categories": safe_list("Asset Category", ["name"]),
        "projects": safe_list("Project", ["name", "project_name"], {"status": "Open"}),
    }


@frappe.whitelist()
def get_customers_list(search=None):
    filters = {"disabled": 0}
    or_filters = None
    if search:
        or_filters = {
            "name": ["like", f"%{search}%"],
            "customer_name": ["like", f"%{search}%"],
        }
    return frappe.get_all(
        "Customer",
        fields=["name", "customer_name", "customer_type", "tax_id", "mobile_no", "email_id"],
        filters=filters,
        or_filters=or_filters,
        order_by="customer_name",
        limit=300,
    )


@frappe.whitelist()
def create_employee(employee_name, gender, date_of_joining, company=None,
                    status="Active", date_of_birth=None, department=None,
                    designation=None, company_email=None, cell_number=None):
    try:
        if not company:
            company = _get_company()
        if not company:
            frappe.throw("No company found.")

        doc = frappe.get_doc({
            "doctype": "Employee",
            "employee_name": employee_name,
            "gender": gender,
            "date_of_joining": date_of_joining or nowdate(),
            "status": status,
            "company": company,
        })
        if date_of_birth:
            doc.date_of_birth = date_of_birth
        if department:
            doc.department = department
        if designation:
            doc.designation = designation
        if company_email:
            doc.company_email = company_email
        if cell_number:
            doc.cell_number = cell_number

        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "employee_name": doc.employee_name}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Employee")
        frappe.throw(str(e))


# ═══════════════════════════════════════════
# HR MODULE APIs
# ═══════════════════════════════════════════

@frappe.whitelist()
def get_hr_dashboard(company=None, from_date=None, to_date=None):
    from frappe.utils import get_first_day, get_last_day, getdate
    if not from_date:
        from_date = get_first_day(getdate(nowdate())).strftime("%Y-%m-%d")
    if not to_date:
        to_date = get_last_day(getdate(nowdate())).strftime("%Y-%m-%d")
    params = {"from_date": from_date, "to_date": to_date}

    def safe_count(dt, filters):
        try: return frappe.db.count(dt, filters) or 0
        except: return 0

    def safe_sql(sql, p):
        try:
            r = frappe.db.sql(sql, p, as_dict=True)
            return r[0] if r else {}
        except: return {}

    emp_count = safe_count("Employee", {"status": "Active"})
    new_hires = safe_count("Employee", {
        "status": "Active",
        "date_of_joining": ["between", [from_date, to_date]]
    })

    att = safe_sql("""
        SELECT
          SUM(status='Present') as present,
          SUM(status='Absent') as absent,
          SUM(status='Half Day') as half_day,
          SUM(status='On Leave') as on_leave,
          SUM(status='Work From Home') as wfh,
          COUNT(*) as total
        FROM `tabAttendance`
        WHERE docstatus=1 AND attendance_date BETWEEN %(from_date)s AND %(to_date)s
    """, params)

    leave_pending = safe_count("Leave Application", {"status": "Open", "docstatus": 0})
    leave_approved = safe_count("Leave Application", {
        "status": "Approved", "docstatus": 1,
        "from_date": ["between", [from_date, to_date]]
    })

    salary = safe_sql("""
        SELECT COALESCE(SUM(net_pay),0) as total, COUNT(*) as cnt
        FROM `tabSalary Slip`
        WHERE docstatus=1 AND start_date >= %(from_date)s AND end_date <= %(to_date)s
    """, params)

    expense = safe_sql("""
        SELECT COALESCE(SUM(total_claimed_amount),0) as total, COUNT(*) as cnt
        FROM `tabExpense Claim`
        WHERE docstatus=1 AND posting_date BETWEEN %(from_date)s AND %(to_date)s
    """, params)

    jobs = safe_count("Job Opening", {"status": "Open"})

    # Dept breakdown
    dept_data = []
    try:
        dept_data = frappe.db.sql("""
            SELECT department, COUNT(*) as cnt
            FROM `tabEmployee` WHERE status='Active' AND department IS NOT NULL
            GROUP BY department ORDER BY cnt DESC LIMIT 8
        """, as_dict=True)
    except: pass

    # Recent joiners
    recent_emp = []
    try:
        recent_emp = frappe.get_all("Employee", filters={"status": "Active"},
            fields=["name","employee_name","department","designation","date_of_joining","gender"],
            order_by="date_of_joining desc", limit=6)
    except: pass

    return {
        "emp_count": emp_count, "new_hires": new_hires,
        "att_present": int(att.get("present") or 0),
        "att_absent": int(att.get("absent") or 0),
        "att_half_day": int(att.get("half_day") or 0),
        "att_on_leave": int(att.get("on_leave") or 0),
        "att_wfh": int(att.get("wfh") or 0),
        "att_total": int(att.get("total") or 0),
        "leave_pending": leave_pending, "leave_approved": leave_approved,
        "salary_total": flt(salary.get("total") or 0),
        "salary_count": int(salary.get("cnt") or 0),
        "expense_total": flt(expense.get("total") or 0),
        "expense_count": int(expense.get("cnt") or 0),
        "job_openings": jobs,
        "dept_data": dept_data,
        "recent_employees": recent_emp,
        "from_date": from_date, "to_date": to_date,
    }


@frappe.whitelist()
def get_employees(search=None, department=None, status="Active"):
    filters = {"status": status}
    if department:
        filters["department"] = department
    fields = ["name","employee_name","department","designation","gender",
              "date_of_joining","cell_number","company_email","status","image"]
    try:
        emps = frappe.get_all("Employee", filters=filters, fields=fields,
                               order_by="employee_name", limit=200)
        if search:
            s = search.lower()
            emps = [e for e in emps if s in (e.employee_name or "").lower()
                    or s in (e.department or "").lower()
                    or s in (e.designation or "").lower()]
        return emps
    except Exception as e:
        frappe.throw(str(e))


@frappe.whitelist()
def get_attendance_log(employee=None, from_date=None, to_date=None):
    from frappe.utils import get_first_day, get_last_day, getdate
    if not from_date:
        from_date = get_first_day(getdate(nowdate())).strftime("%Y-%m-%d")
    if not to_date:
        to_date = get_last_day(getdate(nowdate())).strftime("%Y-%m-%d")
    filters = {"docstatus": 1, "attendance_date": ["between", [from_date, to_date]]}
    if employee:
        filters["employee"] = employee
    try:
        rows = frappe.get_all("Attendance",
            filters=filters,
            fields=["name","employee","employee_name","attendance_date","status",
                    "in_time","out_time","working_hours","department"],
            order_by="attendance_date desc", limit=200)
        return rows
    except Exception as e:
        frappe.throw(str(e))


@frappe.whitelist()
def mark_attendance(employee, attendance_date, status, in_time=None, out_time=None):
    try:
        existing = frappe.db.get_value("Attendance", {
            "employee": employee,
            "attendance_date": attendance_date,
            "docstatus": ["!=", 2]
        }, "name")
        if existing:
            frappe.throw(f"Attendance already marked for {attendance_date}")
        doc = frappe.get_doc({
            "doctype": "Attendance",
            "employee": employee,
            "attendance_date": attendance_date,
            "status": status,
            "in_time": in_time,
            "out_time": out_time,
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return {"name": doc.name, "status": doc.status}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Mark Attendance")
        frappe.throw(str(e))


@frappe.whitelist()
def get_leave_data(employee=None, from_date=None, to_date=None):
    from frappe.utils import get_first_day, get_last_day, getdate
    if not from_date:
        from_date = get_first_day(getdate(nowdate())).strftime("%Y-%m-%d")
    if not to_date:
        to_date = get_last_day(getdate(nowdate())).strftime("%Y-%m-%d")

    apps_filters = {"docstatus": ["!=", 2], "from_date": [">=", from_date]}
    if employee:
        apps_filters["employee"] = employee
    try:
        applications = frappe.get_all("Leave Application",
            filters=apps_filters,
            fields=["name","employee","employee_name","leave_type","from_date","to_date",
                    "total_leave_days","status","description","docstatus"],
            order_by="from_date desc", limit=100)

        leave_types = frappe.get_all("Leave Type", fields=["name","max_leaves_allowed"], limit=50)

        # Allocations — use only fields that exist in tabLeave Allocation
        alloc_filters = {"docstatus": 1, "to_date": [">=", nowdate()]}
        if employee:
            alloc_filters["employee"] = employee
        allocations = frappe.get_all("Leave Allocation",
            filters=alloc_filters,
            fields=["name","employee","employee_name","leave_type",
                    "new_leaves_allocated","carry_forwarded_leaves_count",
                    "total_leaves_allocated","unused_leaves","from_date","to_date"],
            limit=100)

        # Calculate leaves taken from approved Leave Applications (current year)
        import datetime
        cur_year = datetime.date.today().year
        taken_filters = [
            ["docstatus", "=", 1],
            ["status", "=", "Approved"],
            ["from_date", ">=", f"{cur_year}-01-01"],
        ]
        if employee:
            taken_filters.append(["employee", "=", employee])
        taken_apps = frappe.get_all("Leave Application",
            filters=taken_filters,
            fields=["employee","leave_type","total_leave_days"],
            limit=500)

        # Build taken map: {(employee, leave_type): total_days}
        taken_map = {}
        for a in taken_apps:
            k = (a.employee, a.leave_type)
            taken_map[k] = taken_map.get(k, 0) + flt(a.total_leave_days)

        for alloc in allocations:
            taken = taken_map.get((alloc.employee, alloc.leave_type), 0)
            alloc["leaves_taken"] = taken
            alloc["leaves_remaining"] = flt(alloc.total_leaves_allocated) - taken

        return {
            "applications": applications,
            "leave_types": leave_types,
            "allocations": allocations,
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_leave_data")
        frappe.throw(str(e))


@frappe.whitelist()
def get_leave_balance(employee, leave_type):
    import datetime
    cur_year = datetime.date.today().year
    try:
        allocs = frappe.get_all("Leave Allocation",
            filters={"employee": employee, "leave_type": leave_type,
                     "docstatus": 1, "to_date": [">=", nowdate()]},
            fields=["total_leaves_allocated", "carry_forwarded_leaves_count",
                    "new_leaves_allocated", "from_date", "to_date"],
            order_by="to_date desc", limit=1)

        allocated = flt(allocs[0].total_leaves_allocated) if allocs else 0
        from_date_alloc = allocs[0].from_date if allocs else f"{cur_year}-01-01"
        to_date_alloc   = allocs[0].to_date   if allocs else nowdate()

        taken_rows = frappe.db.sql("""
            SELECT COALESCE(SUM(total_leave_days), 0) as taken
            FROM `tabLeave Application`
            WHERE employee=%(emp)s AND leave_type=%(lt)s
              AND status='Approved' AND docstatus=1
              AND from_date >= %(fd)s
        """, {"emp": employee, "lt": leave_type, "fd": f"{cur_year}-01-01"}, as_dict=True)
        taken = flt(taken_rows[0].taken) if taken_rows else 0

        pending_rows = frappe.db.sql("""
            SELECT COALESCE(SUM(total_leave_days), 0) as pending
            FROM `tabLeave Application`
            WHERE employee=%(emp)s AND leave_type=%(lt)s
              AND status='Open' AND docstatus=0
        """, {"emp": employee, "lt": leave_type}, as_dict=True)
        pending = flt(pending_rows[0].pending) if pending_rows else 0

        remaining = allocated - taken
        return {
            "allocated": allocated,
            "taken": taken,
            "pending": pending,
            "remaining": remaining,
            "from_date": str(from_date_alloc),
            "to_date": str(to_date_alloc),
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_leave_balance")
        return {"allocated": 0, "taken": 0, "pending": 0, "remaining": 0}


@frappe.whitelist()
def apply_leave(employee, leave_type, from_date, to_date, reason=None, half_day=0, half_day_date=None):
    try:
        doc = frappe.get_doc({
            "doctype": "Leave Application",
            "employee": employee,
            "leave_type": leave_type,
            "from_date": from_date,
            "to_date": to_date,
            "description": reason,
            "half_day": int(half_day),
            "half_day_date": half_day_date if int(half_day) else None,
            "status": "Open",
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "total_leave_days": doc.total_leave_days, "status": doc.status}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Apply Leave")
        frappe.throw(str(e))


@frappe.whitelist()
def approve_leave(name, action):
    try:
        doc = frappe.get_doc("Leave Application", name)
        doc.flags.ignore_permissions = True
        if action == "Approve":
            doc.status = "Approved"
            doc.docstatus = 1
            doc.submit()
        elif action == "Reject":
            if doc.docstatus == 1:
                doc.cancel()
            else:
                doc.status = "Rejected"
                doc.save(ignore_permissions=True)
        return {"status": doc.status}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Approve Leave")
        frappe.throw(str(e))


@frappe.whitelist()
def get_employee_loan_applications(employee=None):
    filters = {}
    if employee:
        filters["employee"] = employee
    return frappe.get_all(
        "Employee Loan Application",
        filters=filters,
        fields=[
            "name", "employee", "employee_name", "company", "requested_on",
            "loan_type", "requested_amount", "repayment_months",
            "monthly_repayment", "purpose", "status", "decision_notes",
        ],
        order_by="requested_on desc, creation desc",
        limit=200,
    )


@frappe.whitelist(methods=["POST"])
def apply_employee_loan(employee, loan_type, requested_amount,
                        repayment_months, purpose, requested_on=None):
    doc = frappe.get_doc({
        "doctype": "Employee Loan Application",
        "employee": employee,
        "requested_on": requested_on or nowdate(),
        "loan_type": loan_type,
        "requested_amount": flt(requested_amount),
        "repayment_months": cint(repayment_months),
        "purpose": cstr(purpose).strip(),
        "status": "Pending Approval",
    })
    doc.insert()
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
def decide_employee_loan(name, status, decision_notes=None):
    if status not in ("Approved", "Rejected"):
        frappe.throw("Status must be Approved or Rejected.")
    if not ({"HR Manager", "System Manager"} & set(frappe.get_roles())):
        frappe.throw("Only an HR Manager can decide a loan application.", frappe.PermissionError)
    doc = frappe.get_doc("Employee Loan Application", name)
    doc.status = status
    doc.decision_notes = decision_notes
    doc.save()
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def get_salary_slips(employee=None, from_year=None):
    # Show submitted + draft (exclude cancelled)
    filters = {"docstatus": ["!=", 2]}
    if employee:
        filters["employee"] = employee
    if from_year:
        filters["start_date"] = [">=", f"{from_year}-01-01"]
    try:
        slips = frappe.get_all("Salary Slip",
            filters=filters,
            fields=["name","employee","employee_name","start_date","end_date",
                    "net_pay","gross_pay","total_deduction","payment_days","department","docstatus","status"],
            order_by="start_date desc", limit=36)
        return slips
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_salary_slips")
        frappe.throw(str(e))


@frappe.whitelist()
def get_expense_claims(employee=None):
    filters = {"docstatus": ["!=", 2]}
    if employee:
        filters["employee"] = employee
    try:
        claims = frappe.get_all("Expense Claim",
            filters=filters,
            fields=["name","employee","employee_name","posting_date","total_claimed_amount",
                    "total_sanctioned_amount","status","docstatus","expense_approver"],
            order_by="posting_date desc", limit=100)
        return claims
    except Exception as e:
        frappe.throw(str(e))


@frappe.whitelist()
def create_expense_claim(employee, posting_date, expenses, expense_approver=None, remark=None, company=None):
    try:
        if not company:
            company = _get_company()
        expenses = json.loads(expenses) if isinstance(expenses, str) else expenses
        doc = frappe.get_doc({
            "doctype": "Expense Claim",
            "employee": employee,
            "posting_date": posting_date,
            "company": company,
            "expense_approver": expense_approver,
            "remark": remark,
            "expenses": [
                {
                    "expense_date": e.get("expense_date", posting_date),
                    "expense_type": e.get("expense_type"),
                    "description": e.get("description"),
                    "amount": flt(e.get("amount", 0)),
                    "sanctioned_amount": flt(e.get("amount", 0)),
                }
                for e in expenses if e.get("expense_type") and flt(e.get("amount", 0)) > 0
            ],
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "total_claimed_amount": doc.total_claimed_amount}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Expense Claim")
        frappe.throw(str(e))


@frappe.whitelist()
def get_job_openings():
    try:
        jobs = frappe.get_all("Job Opening",
            filters={"status": "Open"},
            fields=["name","job_title","department","designation",
                    "planned_vacancies","vacancies",
                    "description","posted_on","closes_on","company",
                    "employment_type","location"],
            order_by="posted_on desc", limit=50)
        # normalise vacancies field name for frontend
        for j in jobs:
            j["no_of_positions"] = j.get("planned_vacancies") or j.get("vacancies") or 1
        return jobs
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_job_openings")
        frappe.throw(str(e))


@frappe.whitelist()
def get_employee_details(employee):
    """Full profile: info + last 6 salary slips + leave balances + leave taken."""
    try:
        import datetime
        cur_year = datetime.date.today().year

        emp = frappe.get_value("Employee", employee,
            ["name","employee_name","department","designation","company",
             "date_of_joining","employment_type","gender","cell_number",
             "personal_email","company_email","branch","grade","status"],
            as_dict=True)
        if not emp:
            frappe.throw(f"Employee {employee} not found")

        # Last 6 salary slips
        slips = frappe.get_all("Salary Slip",
            filters={"employee": employee, "docstatus": ["!=", 2]},
            fields=["name","start_date","end_date","net_pay","gross_pay",
                    "total_deduction","payment_days","docstatus","status"],
            order_by="start_date desc", limit=6)

        # Leave allocations (current/active)
        allocations = frappe.get_all("Leave Allocation",
            filters={"employee": employee, "docstatus": 1,
                     "to_date": [">=", nowdate()]},
            fields=["leave_type","total_leaves_allocated","new_leaves_allocated",
                    "carry_forwarded_leaves_count","from_date","to_date"],
            limit=30)

        # Leaves taken this year per type
        taken_rows = frappe.db.sql("""
            SELECT leave_type, SUM(total_leave_days) as taken
            FROM `tabLeave Application`
            WHERE employee=%s AND docstatus=1 AND status='Approved'
              AND YEAR(from_date)=%s
            GROUP BY leave_type
        """, (employee, cur_year), as_dict=True)
        taken_map = {r.leave_type: flt(r.taken) for r in taken_rows}

        leave_summary = []
        for a in allocations:
            taken = taken_map.get(a.leave_type, 0)
            remaining = flt(a.total_leaves_allocated) - taken
            leave_summary.append({
                "leave_type": a.leave_type,
                "allocated": flt(a.total_leaves_allocated),
                "taken": taken,
                "remaining": max(remaining, 0),
                "pending": 0,
            })

        # Pending (Open/Submitted) leave applications
        pending = frappe.get_all("Leave Application",
            filters={"employee": employee, "docstatus": ["!=", 2], "status": ["in", ["Open","Submitted"]]},
            fields=["leave_type","total_leave_days","from_date","to_date"],
            order_by="from_date desc", limit=10)
        pending_map = {}
        for p in pending:
            pending_map[p.leave_type] = pending_map.get(p.leave_type, 0) + flt(p.total_leave_days)
        for ls in leave_summary:
            ls["pending"] = pending_map.get(ls["leave_type"], 0)

        # Attendance this month
        import datetime as dt
        today = dt.date.today()
        month_start = today.replace(day=1).strftime("%Y-%m-%d")
        att = frappe.db.sql("""
            SELECT status, COUNT(*) as cnt FROM `tabAttendance`
            WHERE employee=%s AND docstatus=1 AND attendance_date>=%s
            GROUP BY status
        """, (employee, month_start), as_dict=True)
        att_summary = {r.status: r.cnt for r in att}

        return {
            "employee": emp,
            "salary_slips": slips,
            "leave_summary": leave_summary,
            "pending_leaves": pending,
            "attendance_this_month": att_summary,
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_employee_details")
        frappe.throw(str(e))


@frappe.whitelist()
def run_payroll(company, start_date, end_date, payroll_frequency="Monthly", department=None, branch=None, cost_center=None):
    """Create a Payroll Entry draft for the given period."""
    try:
        if not company:
            company = _get_company()
        doc = frappe.get_doc({
            "doctype": "Payroll Entry",
            "company": company,
            "start_date": start_date,
            "end_date": end_date,
            "payroll_frequency": payroll_frequency,
            "payment_account": None,
        })
        if department:
            doc.department = department
        if branch:
            doc.branch = branch
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        # Fill employees
        try:
            doc.fill_employee_details()
            doc.save(ignore_permissions=True)
        except Exception:
            pass
        return {"name": doc.name, "employee_count": len(doc.get("employees", []))}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: run_payroll")
        frappe.throw(str(e))


@frappe.whitelist()
def get_salary_structures():
    """List all salary structures with earnings/deduction totals."""
    try:
        structures = frappe.db.sql("""
            SELECT name, payroll_frequency, is_active, is_default,
                   total_earning, total_deduction, currency
            FROM `tabSalary Structure`
            ORDER BY modified DESC
        """, as_dict=True)
        return structures
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_salary_structures")
        return []


@frappe.whitelist()
def get_salary_structure_detail(name):
    """Get a salary structure with its earnings and deduction components."""
    try:
        ss = frappe.get_doc("Salary Structure", name)
        earnings = []
        deductions = []
        for row in ss.get("earnings", []):
            earnings.append({
                "salary_component": row.salary_component,
                "abbr": row.abbr,
                "amount": flt(row.amount),
                "formula": row.formula or "",
                "condition": row.condition or "",
            })
        for row in ss.get("deductions", []):
            deductions.append({
                "salary_component": row.salary_component,
                "abbr": row.abbr,
                "amount": flt(row.amount),
                "formula": row.formula or "",
                "condition": row.condition or "",
            })
        return {
            "name": ss.name,
            "payroll_frequency": ss.payroll_frequency,
            "is_active": ss.is_active,
            "is_default": ss.is_default,
            "currency": ss.currency,
            "total_earning": flt(ss.total_earning),
            "total_deduction": flt(ss.total_deduction),
            "earnings": earnings,
            "deductions": deductions,
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_salary_structure_detail")
        frappe.throw(f"Could not load salary structure: {name}")


@frappe.whitelist()
def get_salary_components(component_type=None):
    """Get all salary components, optionally filtered by type (Earning / Deduction)."""
    try:
        filters = {}
        if component_type:
            filters["type"] = component_type
        comps = frappe.get_all(
            "Salary Component",
            filters=filters,
            fields=["name", "type as salary_component_type",
                    "salary_component_abbr as abbr", "description"],
            order_by="type asc, name asc"
        )
        return comps
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_salary_components")
        return []


def _new_salary_component(component_name, component_type, uses_formula=False):
    """Create a minimal payroll component when it is entered from the portal."""
    import re

    words = re.findall(r"[A-Za-z0-9]+", cstr(component_name))
    base = "".join(word[0] for word in words).upper()[:5] or "SC"
    abbr = base
    suffix = 1
    while frappe.db.exists("Salary Component", {"salary_component_abbr": abbr}):
        suffix += 1
        abbr = f"{base[:4]}{suffix}"[:5]
    doc = frappe.get_doc({
        "doctype": "Salary Component",
        "salary_component": cstr(component_name).strip(),
        "salary_component_abbr": abbr,
        "type": component_type,
        "depends_on_payment_days": 0 if uses_formula else 1,
        "is_tax_applicable": 1 if component_type == "Earning" else 0,
    })
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist(methods=["POST"])
def create_salary_component(component_name, component_type, abbr=None,
                            description=None, depends_on_payment_days=None,
                            is_tax_applicable=None):
    """Create a Salary Component from the portal."""
    if component_type not in ("Earning", "Deduction"):
        frappe.throw("Component type must be Earning or Deduction.")
    name = cstr(component_name).strip()
    if not name:
        frappe.throw("Component name is required.")
    if frappe.db.exists("Salary Component", name):
        frappe.throw("A salary component named '%s' already exists." % name)

    import re
    if abbr:
        abbr = cstr(abbr).strip().upper()[:5]
    else:
        words = re.findall(r"[A-Za-z0-9]+", name)
        abbr = ("".join(w[0] for w in words).upper()[:5]) or "SC"
    base, suffix = abbr, 1
    while frappe.db.exists("Salary Component", {"salary_component_abbr": abbr}):
        suffix += 1
        abbr = f"{base[:4]}{suffix}"[:5]

    doc = frappe.get_doc({
        "doctype": "Salary Component",
        "salary_component": name,
        "salary_component_abbr": abbr,
        "type": component_type,
        "description": cstr(description or "").strip(),
        "depends_on_payment_days": cint(depends_on_payment_days) if depends_on_payment_days is not None
                                   else (1 if component_type == "Earning" else 0),
        "is_tax_applicable": cint(is_tax_applicable) if is_tax_applicable is not None
                             else (1 if component_type == "Earning" else 0),
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"name": doc.name, "abbr": abbr, "type": component_type}


@frappe.whitelist(methods=["POST"])
def create_salary_structure(name, payroll_frequency, earnings, deductions, company=None):
    """Create and submit a salary structure entirely from portal input."""
    import json
    try:
        if not company:
            company = _get_company()
        earnings = json.loads(earnings) if isinstance(earnings, str) else (earnings or [])
        deductions = json.loads(deductions) if isinstance(deductions, str) else (deductions or [])
        payroll_frequency = {"Biweekly": "Fortnightly"}.get(payroll_frequency, payroll_frequency)
        valid_frequencies = ("Monthly", "Fortnightly", "Bimonthly", "Weekly", "Daily")
        if payroll_frequency not in valid_frequencies:
            frappe.throw("Invalid payroll frequency.")
        if not cstr(name).strip():
            frappe.throw("Structure name is required.")
        if frappe.db.exists("Salary Structure", cstr(name).strip()):
            frappe.throw(f"Salary Structure {name} already exists.")
        if not earnings:
            frappe.throw("Add at least one earning component.")

        seen = set()
        for component_type, rows in (("Earning", earnings), ("Deduction", deductions)):
            for row in rows:
                component = cstr(row.get("salary_component")).strip()
                if not component:
                    frappe.throw(f"Select or enter a {component_type.lower()} component.")
                if flt(row.get("amount")) <= 0 and not cstr(row.get("formula")).strip():
                    frappe.throw(f"Salary Component {component} needs a positive amount or a formula.")
                key = component.lower()
                if key in seen:
                    frappe.throw(f"Salary Component {component} is added more than once.")
                seen.add(key)
                existing_type = frappe.db.get_value("Salary Component", component, "type")
                if existing_type and existing_type != component_type:
                    frappe.throw(f"Salary Component {component} is a {existing_type}, not a {component_type}.")
                if not existing_type:
                    row["salary_component"] = _new_salary_component(
                        component, component_type, bool(cstr(row.get("formula")).strip())
                    )

        currency = frappe.db.get_value("Company", company, "default_currency")
        if not currency:
            frappe.throw(f"Set a default currency for Company {company} first.")

        def salary_row(row):
            formula = cstr(row.get("formula")).strip()
            return {
                "salary_component": row["salary_component"],
                "amount": 0 if formula else flt(row.get("amount", 0)),
                "amount_based_on_formula": 1 if formula else 0,
                "formula": formula,
                "condition": cstr(row.get("condition")).strip(),
            }

        doc = frappe.get_doc({
            "doctype": "Salary Structure",
            "name": cstr(name).strip(),
            "company": company,
            "currency": currency,
            "payroll_frequency": payroll_frequency,
            "is_active": "Yes",
            "earnings": [salary_row(e) for e in earnings],
            "deductions": [salary_row(d) for d in deductions],
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return {
            "name": doc.name,
            "ok": True,
            "total_earning": flt(doc.total_earning),
            "total_deduction": flt(doc.total_deduction),
            "currency": doc.currency,
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: create_salary_structure")
        frappe.throw(str(e))


@frappe.whitelist()
def get_salary_structure_assignments(employee=None):
    """Get salary structure assignments, optionally filtered by employee."""
    try:
        filters = {}
        if employee:
            filters["employee"] = employee
        rows = frappe.db.sql("""
            SELECT ssa.name, ssa.employee,
                   emp.employee_name, emp.department,
                   ssa.salary_structure, ssa.from_date, ssa.base, ssa.currency
            FROM `tabSalary Structure Assignment` ssa
            LEFT JOIN `tabEmployee` emp ON emp.name = ssa.employee
            WHERE ssa.docstatus < 2
            ORDER BY ssa.from_date DESC
            LIMIT 200
        """, as_dict=True)
        if employee:
            rows = [r for r in rows if r.employee == employee]
        return rows
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_salary_structure_assignments")
        return []


@frappe.whitelist()
def assign_salary_structure(employee, salary_structure, from_date, base):
    """Assign a salary structure to an employee."""
    try:
        company = frappe.db.get_value("Employee", employee, "company") or _get_company()
        doc = frappe.get_doc({
            "doctype": "Salary Structure Assignment",
            "employee": employee,
            "salary_structure": salary_structure,
            "from_date": from_date,
            "base": flt(base),
            "company": company,
        })
        doc.insert(ignore_permissions=True)
        doc.submit()
        return {"name": doc.name, "ok": True}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: assign_salary_structure")
        frappe.throw(str(e))


@frappe.whitelist()
def ocr_expense_receipt(image_base64, filename=None):
    """Use Claude claude-haiku-4-5-20251001 vision to extract expense data from a receipt image."""
    try:
        import base64, re
        # Strip data URL prefix if present
        if "," in image_base64:
            image_base64 = image_base64.split(",", 1)[1]

        # Determine media type from filename or default to jpeg
        media_type = "image/jpeg"
        if filename:
            fn_lower = filename.lower()
            if fn_lower.endswith(".png"):
                media_type = "image/png"
            elif fn_lower.endswith(".gif"):
                media_type = "image/gif"
            elif fn_lower.endswith(".webp"):
                media_type = "image/webp"

        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_base64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Extract expense information from this receipt. "
                            "Return ONLY a JSON object with these fields (no markdown, no explanation): "
                            "{\"vendor\": \"...\", \"date\": \"YYYY-MM-DD\", \"amount\": 0.00, "
                            "\"currency\": \"AED\", \"category\": \"...\", \"description\": \"...\"}. "
                            "If a field is not found, use null. Amount must be a number."
                        )
                    }
                ],
            }],
        )
        raw = msg.content[0].text.strip()
        # Parse JSON from response
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            import json as _json
            data = _json.loads(match.group())
            return {"ok": True, "data": data, "raw": raw}
        return {"ok": False, "raw": raw, "data": {}}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: ocr_expense_receipt")
        return {"ok": False, "error": str(e), "data": {}}


@frappe.whitelist()
def get_expense_approvers():
    """Return users who can approve expense claims (HR Manager, HR User, Accounts Manager roles)."""
    try:
        approver_roles = ["HR Manager", "HR User", "Accounts Manager", "Accounts User", "System Manager"]
        users = frappe.db.sql("""
            SELECT DISTINCT u.name, u.full_name, u.email
            FROM `tabUser` u
            INNER JOIN `tabHas Role` hr ON hr.parent = u.name
            WHERE hr.role IN %(roles)s
              AND u.enabled = 1
              AND u.name NOT IN ('Administrator', 'Guest')
            ORDER BY u.full_name
            LIMIT 100
        """, {"roles": approver_roles}, as_dict=True)
        return users
    except Exception as e:
        # Fallback: return all non-guest active users
        try:
            return frappe.get_all("User",
                filters={"enabled": 1, "name": ["not in", ["Administrator", "Guest"]]},
                fields=["name", "full_name", "email"],
                order_by="full_name asc",
                limit=100)
        except:
            return []


@frappe.whitelist()
def get_hr_notices():
    """HR Announcements — uses Frappe Notice/Blog if available, else returns empty."""
    try:
        notices = []
        try:
            notices = frappe.get_all("HR Notice",
                filters={"status": "Active"},
                fields=["name","title","notice_date","description","notified_on"],
                order_by="notice_date desc", limit=20)
        except Exception:
            pass
        if not notices:
            try:
                notices = frappe.get_all("Communication",
                    filters={"reference_doctype": "Employee", "communication_type": "Communication"},
                    fields=["name","subject","content","sent_or_received","creation"],
                    order_by="creation desc", limit=10)
            except Exception:
                pass
        return notices
    except Exception as e:
        return []


@frappe.whitelist()
def get_leave_types():
    try:
        return frappe.get_all("Leave Type", fields=["name","max_leaves_allowed"], limit=50)
    except: return []


@frappe.whitelist()
def get_expense_types():
    try:
        return frappe.get_all("Expense Claim Type", fields=["name","default_account"], limit=50)
    except: return []


@frappe.whitelist()
def employee_checkin(employee, log_type, time=None, latitude=None, longitude=None, device_id=None, skip_auto_attendance=0):
    """Create an Employee Checkin record (HRMS). log_type = 'IN' or 'OUT'."""
    try:
        from frappe.utils import now_datetime, get_datetime
        import datetime

        emp_doc = frappe.get_value("Employee", employee, ["name", "employee_name", "company"], as_dict=True)
        if not emp_doc:
            frappe.throw(f"Employee '{employee}' not found.")

        checkin_time = get_datetime(time) if time else now_datetime()

        doc = frappe.new_doc("Employee Checkin")
        doc.employee = emp_doc.name
        doc.employee_name = emp_doc.employee_name
        doc.log_type = log_type   # 'IN' or 'OUT'
        doc.time = checkin_time
        if latitude:
            doc.latitude = flt(latitude)
        if longitude:
            doc.longitude = flt(longitude)
        if device_id:
            doc.device_id = cstr(device_id)
        doc.skip_auto_attendance = int(skip_auto_attendance)
        doc.flags.ignore_permissions = True
        doc.insert()
        return {
            "name": doc.name,
            "employee": doc.employee,
            "employee_name": doc.employee_name,
            "log_type": doc.log_type,
            "time": str(doc.time),
            "latitude": doc.latitude,
            "longitude": doc.longitude,
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "employee_checkin failed")
        frappe.throw(str(e))


@frappe.whitelist()
def get_checkin_log(employee=None, from_date=None, to_date=None):
    """Return Employee Checkin records with paired IN-OUT durations."""
    try:
        from frappe.utils import nowdate, add_days
        if not to_date:
            to_date = nowdate()
        if not from_date:
            from_date = add_days(to_date, -7)

        emp_clause = "AND employee = %(employee)s" if employee else ""
        sql = """
            SELECT name, employee, employee_name, log_type,
                   time, latitude, longitude, device_id
            FROM `tabEmployee Checkin`
            WHERE DATE(time) BETWEEN %(from_date)s AND %(to_date)s
            {emp_clause}
            ORDER BY time ASC
        """.format(emp_clause=emp_clause)

        params = {"from_date": from_date, "to_date": to_date}
        if employee:
            params["employee"] = employee

        records = frappe.db.sql(sql, params, as_dict=True)

        # Pair each IN with the next OUT (same employee, same day) and add duration_minutes
        # Records are ASC by time; iterate and pair greedily
        pending_in = {}  # employee -> last IN record
        for r in records:
            r["time"] = str(r["time"]) if r["time"] else ""
            r["duration_minutes"] = None
            r["paired_with"] = None
            r["latitude"] = float(r["latitude"] or 0)
            r["longitude"] = float(r["longitude"] or 0)

            emp = r["employee"]
            if r["log_type"] == "IN":
                pending_in[emp] = r
            elif r["log_type"] == "OUT" and emp in pending_in:
                in_rec = pending_in.pop(emp)
                try:
                    import datetime
                    t_in  = datetime.datetime.strptime(in_rec["time"][:19], "%Y-%m-%d %H:%M:%S")
                    t_out = datetime.datetime.strptime(r["time"][:19],      "%Y-%m-%d %H:%M:%S")
                    diff_mins = int((t_out - t_in).total_seconds() / 60)
                    if diff_mins > 0:
                        r["duration_minutes"] = diff_mins
                        r["paired_with"]      = in_rec["name"]
                        in_rec["duration_minutes"] = diff_mins
                except Exception:
                    pass

        # Return descending (most recent first)
        return list(reversed(records))
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_checkin_log")
        return []


@frappe.whitelist()
def get_employee_for_user():
    """Return the Employee linked to the currently logged-in user.
    Also returns all active employees so admins can select manually."""
    try:
        emp = frappe.db.get_value(
            "Employee",
            {"user_id": frappe.session.user, "status": "Active"},
            ["name", "employee_name", "department", "designation", "company"],
            as_dict=True
        )
        all_employees = frappe.get_all(
            "Employee",
            filters={"status": "Active"},
            fields=["name", "employee_name", "department", "designation", "company"],
            order_by="employee_name asc",
            limit=200
        )
        is_admin = "System Manager" in frappe.get_roles(frappe.session.user)
        return {
            "linked": emp or None,
            "all_employees": all_employees,
            "is_admin": is_admin
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "get_employee_for_user failed")
        return {"linked": None, "all_employees": [], "is_admin": False}


# ─────────────────────────── CRM ───────────────────────────

@frappe.whitelist()
def get_leads(search=None, status=None):
    try:
        cond, vals = "WHERE 1=1", {}
        if search:
            cond += " AND (l.lead_name LIKE %(s)s OR l.company_name LIKE %(s)s OR l.email_id LIKE %(s)s)"
            vals["s"] = f"%{search}%"
        if status:
            cond += " AND l.status = %(status)s"
            vals["status"] = status
        return frappe.db.sql(f"""
            SELECT l.name, l.lead_name, l.company_name, l.status, l.source,
                   l.email_id, l.mobile_no, l.lead_owner, l.creation
            FROM `tabLead` l {cond}
            ORDER BY l.creation DESC LIMIT 300
        """, vals, as_dict=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_leads")
        return []

@frappe.whitelist()
def create_lead(lead_name, company_name=None, email_id=None, mobile_no=None,
                source=None, status="Lead", notes=None):
    try:
        doc = frappe.get_doc({
            "doctype": "Lead",
            "lead_name": lead_name,
            "company_name": company_name or "",
            "email_id": email_id or "",
            "mobile_no": mobile_no or "",
            "source": source or "",
            "status": status,
            "notes": notes or "",
        })
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "ok": True}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: create_lead")
        frappe.throw(str(e))

@frappe.whitelist()
def update_lead_status(name, status):
    try:
        frappe.db.set_value("Lead", name, "status", status)
        frappe.db.commit()
        return {"ok": True}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: update_lead_status")
        frappe.throw(str(e))

@frappe.whitelist()
def get_opportunities(search=None, stage=None):
    try:
        cond, vals = "WHERE o.docstatus < 2", {}
        if search:
            cond += " AND (o.title LIKE %(s)s OR o.party_name LIKE %(s)s)"
            vals["s"] = f"%{search}%"
        if stage:
            cond += " AND o.sales_stage = %(stage)s"
            vals["stage"] = stage
        return frappe.db.sql(f"""
            SELECT o.name, o.title, o.opportunity_from, o.party_name,
                   o.opportunity_type, o.opportunity_amount, o.currency,
                   o.sales_stage, o.probability, o.expected_closing,
                   o.contact_person, o.contact_email, o.creation
            FROM `tabOpportunity` o {cond}
            ORDER BY o.expected_closing ASC LIMIT 300
        """, vals, as_dict=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_opportunities")
        return []

@frappe.whitelist()
def create_opportunity(opportunity_from, party_name, opportunity_type=None,
                       opportunity_amount=None, probability=None,
                       expected_closing=None, sales_stage=None, remarks=None):
    try:
        doc = frappe.get_doc({
            "doctype": "Opportunity",
            "opportunity_from": opportunity_from or "Lead",
            "party_name": party_name,
            "opportunity_type": opportunity_type or "Sales",
            "opportunity_amount": flt(opportunity_amount or 0),
            "probability": flt(probability or 20),
            "expected_closing": expected_closing or frappe.utils.add_months(frappe.utils.nowdate(), 1),
            "sales_stage": sales_stage or "Prospecting",
            "remarks": remarks or "",
        })
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "ok": True}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: create_opportunity")
        frappe.throw(str(e))

@frappe.whitelist()
def update_opportunity_stage(name, sales_stage, probability=None):
    try:
        upd = {"sales_stage": sales_stage}
        if probability is not None:
            upd["probability"] = flt(probability)
        frappe.db.set_value("Opportunity", name, upd)
        frappe.db.commit()
        return {"ok": True}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: update_opportunity_stage")
        frappe.throw(str(e))


# ─────────────────────────── SALES EXTRAS ───────────────────────────

@frappe.whitelist()
def get_quotations(search=None, status=None, from_date=None, to_date=None):
    try:
        cond, vals = "WHERE q.docstatus < 2", {}
        if search:
            cond += " AND (q.name LIKE %(s)s OR q.party_name LIKE %(s)s)"
            vals["s"] = f"%{search}%"
        if status:
            cond += " AND q.status = %(status)s"
            vals["status"] = status
        if from_date:
            cond += " AND DATE(q.transaction_date) >= %(fd)s"; vals["fd"] = from_date
        if to_date:
            cond += " AND DATE(q.transaction_date) <= %(td)s"; vals["td"] = to_date
        return frappe.db.sql(f"""
            SELECT q.name, q.quotation_to, q.party_name, q.transaction_date,
                   q.valid_till, q.grand_total, q.currency, q.status, q.docstatus
            FROM `tabQuotation` q {cond}
            ORDER BY q.transaction_date DESC LIMIT 200
        """, vals, as_dict=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_quotations")
        return []

@frappe.whitelist()
def create_quotation(party_type, party_name, transaction_date, items, valid_till=None,
                     remarks=None, company=None):
    import json
    try:
        if not company:
            company = _get_company()
        items = json.loads(items) if isinstance(items, str) else items
        doc = frappe.get_doc({
            "doctype": "Quotation",
            "quotation_to": party_type,
            "party_name": party_name,
            "transaction_date": transaction_date,
            "valid_till": valid_till or frappe.utils.add_days(transaction_date, 30),
            "company": company,
            "remarks": remarks or "",
            "items": [
                {"item_code": i["item_code"], "qty": flt(i.get("qty", 1)),
                 "rate": flt(i.get("rate", 0)), "description": i.get("description", "")}
                for i in items
            ],
        })
        doc.insert(ignore_permissions=True)
        doc.submit()
        return {"name": doc.name, "grand_total": flt(doc.grand_total), "ok": True}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: create_quotation")
        frappe.throw(str(e))

@frappe.whitelist()
def get_payment_entries(party_type=None, party=None, from_date=None, to_date=None):
    try:
        cond, vals = "WHERE pe.docstatus < 2", {}
        if party_type:
            cond += " AND pe.party_type = %(pt)s"; vals["pt"] = party_type
        if party:
            cond += " AND pe.party = %(p)s"; vals["p"] = party
        if from_date:
            cond += " AND DATE(pe.posting_date) >= %(fd)s"; vals["fd"] = from_date
        if to_date:
            cond += " AND DATE(pe.posting_date) <= %(td)s"; vals["td"] = to_date
        return frappe.db.sql(f"""
            SELECT pe.name, pe.payment_type, pe.party_type, pe.party, pe.party_name,
                   pe.posting_date, pe.paid_amount,
                   CASE WHEN pe.payment_type='Receive' THEN pe.paid_to_account_currency
                        ELSE pe.paid_from_account_currency END AS currency,
                   pe.reference_no,
                   pe.mode_of_payment, pe.docstatus
            FROM `tabPayment Entry` pe {cond}
            ORDER BY pe.posting_date DESC LIMIT 200
        """, vals, as_dict=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_payment_entries")
        return []


# ─────────────────────────── BUYING ───────────────────────────

@frappe.whitelist()
def get_suppliers(search=None):
    try:
        cond, vals = "", {}
        if search:
            cond = "AND (s.supplier_name LIKE %(s)s OR s.name LIKE %(s)s)"
            vals["s"] = f"%{search}%"
        return frappe.db.sql(f"""
            SELECT s.name, s.supplier_name, s.supplier_type, s.supplier_group,
                   s.country, s.mobile_no, s.email_id
            FROM `tabSupplier` s
            WHERE s.disabled=0 {cond}
            ORDER BY s.supplier_name ASC LIMIT 200
        """, vals, as_dict=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_suppliers")
        return []

@frappe.whitelist()
def get_purchase_orders(search=None, from_date=None, to_date=None, status=None):
    try:
        cond, vals = "WHERE po.docstatus < 2", {}
        if search:
            cond += " AND (po.name LIKE %(s)s OR po.supplier LIKE %(s)s OR po.supplier_name LIKE %(s)s)"
            vals["s"] = f"%{search}%"
        if from_date:
            cond += " AND DATE(po.transaction_date) >= %(fd)s"; vals["fd"] = from_date
        if to_date:
            cond += " AND DATE(po.transaction_date) <= %(td)s"; vals["td"] = to_date
        if status:
            cond += " AND po.status = %(status)s"; vals["status"] = status
        return frappe.db.sql(f"""
            SELECT po.name, po.supplier, po.supplier_name, po.transaction_date,
                   po.schedule_date, po.grand_total, po.currency, po.status, po.per_received
            FROM `tabPurchase Order` po {cond}
            ORDER BY po.transaction_date DESC LIMIT 200
        """, vals, as_dict=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_purchase_orders")
        return []

@frappe.whitelist()
def get_purchase_invoices(search=None, from_date=None, to_date=None):
    try:
        cond, vals = "WHERE pi.docstatus < 2", {}
        if search:
            cond += " AND (pi.name LIKE %(s)s OR pi.supplier LIKE %(s)s OR pi.supplier_name LIKE %(s)s)"
            vals["s"] = f"%{search}%"
        if from_date:
            cond += " AND DATE(pi.posting_date) >= %(fd)s"; vals["fd"] = from_date
        if to_date:
            cond += " AND DATE(pi.posting_date) <= %(td)s"; vals["td"] = to_date
        return frappe.db.sql(f"""
            SELECT pi.name, pi.supplier, pi.supplier_name, pi.posting_date,
                   pi.due_date, pi.grand_total, pi.outstanding_amount, pi.currency,
                   pi.status, pi.bill_no
            FROM `tabPurchase Invoice` pi {cond}
            ORDER BY pi.posting_date DESC LIMIT 200
        """, vals, as_dict=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_purchase_invoices")
        return []


# ─────────────────────────── FIXED ASSETS ───────────────────────────

@frappe.whitelist()
def get_assets(search=None, asset_category=None):
    try:
        cond, vals = "WHERE a.docstatus < 2", {}
        if search:
            cond += " AND (a.asset_name LIKE %(s)s OR a.name LIKE %(s)s)"
            vals["s"] = f"%{search}%"
        if asset_category:
            cond += " AND a.asset_category = %(cat)s"; vals["cat"] = asset_category
        return frappe.db.sql(f"""
            SELECT a.name, a.asset_name, a.asset_category, a.status,
                   a.purchase_date, a.gross_purchase_amount,
                   GREATEST(a.gross_purchase_amount - a.value_after_depreciation, 0)
                       AS accumulated_depreciation_amount,
                   a.value_after_depreciation AS net_asset_value,
                   a.location, a.custodian
            FROM `tabAsset` a {cond}
            ORDER BY a.purchase_date DESC LIMIT 200
        """, vals, as_dict=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_assets")
        return []

@frappe.whitelist()
def get_asset_categories():
    try:
        return frappe.get_all("Asset Category", fields=["name"], order_by="name", limit=100)
    except Exception:
        return []

@frappe.whitelist()
def get_asset_depreciation_schedule(asset_name):
    try:
        rows = frappe.db.sql("""
            SELECT ads.schedule_date, ads.depreciation_amount, ads.accumulated_depreciation_amount,
                   ads.journal_entry
            FROM `tabDepreciation Schedule` ads
            INNER JOIN `tabAsset Depreciation Schedule` parent
                ON parent.name = ads.parent
            WHERE parent.asset = %(name)s AND parent.docstatus < 2
            ORDER BY ads.schedule_date ASC
        """, {"name": asset_name}, as_dict=True)
        return rows
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_asset_depreciation_schedule")
        return []


# ─────────────────────────── STOCK ───────────────────────────

def _require_stock_permission(permtype="read"):
    if not frappe.has_permission("Item", ptype=permtype):
        frappe.throw(
            f"You do not have permission to {permtype} stock items.",
            frappe.PermissionError,
        )


@frappe.whitelist()
def get_stock_dashboard(company=None):
    """Return the compact stock snapshot used by the firm portal."""
    _require_stock_permission()
    company = company or _get_company()
    params = {"company": company}
    summary = frappe.db.sql("""
        SELECT COUNT(DISTINCT CASE WHEN i.disabled=0 AND i.is_stock_item=1 THEN i.name END) AS stock_items,
               COUNT(DISTINCT CASE WHEN IFNULL(b.actual_qty,0) <= i.safety_stock THEN i.name END) AS low_stock_items,
               IFNULL(SUM(b.actual_qty), 0) AS total_qty,
               IFNULL(SUM(b.stock_value), 0) AS stock_value
        FROM `tabItem` i
        LEFT JOIN (
            SELECT b.item_code, SUM(b.actual_qty) AS actual_qty,
                   SUM(b.stock_value) AS stock_value
            FROM `tabBin` b
            INNER JOIN `tabWarehouse` w ON w.name=b.warehouse
            WHERE w.company=%(company)s
            GROUP BY b.item_code
        ) b ON b.item_code=i.name
        WHERE i.disabled=0 AND i.is_stock_item=1
    """, params, as_dict=True)[0]
    warehouses = frappe.db.sql("""
        SELECT w.name AS warehouse, IFNULL(SUM(b.actual_qty),0) AS actual_qty,
               IFNULL(SUM(b.stock_value),0) AS stock_value,
               COUNT(DISTINCT CASE WHEN b.actual_qty != 0 THEN b.item_code END) AS item_count
        FROM `tabWarehouse` w
        LEFT JOIN `tabBin` b ON b.warehouse=w.name
        WHERE w.company=%(company)s AND w.disabled=0 AND w.is_group=0
        GROUP BY w.name
        ORDER BY stock_value DESC, w.name
    """, params, as_dict=True)
    recent = frappe.db.sql("""
        SELECT sle.posting_date, sle.posting_time, sle.item_code, i.item_name,
               sle.warehouse, sle.actual_qty, sle.qty_after_transaction,
               sle.voucher_type, sle.voucher_no
        FROM `tabStock Ledger Entry` sle
        LEFT JOIN `tabItem` i ON i.name=sle.item_code
        WHERE sle.company=%(company)s AND sle.is_cancelled=0
        ORDER BY sle.posting_date DESC, sle.posting_time DESC, sle.creation DESC
        LIMIT 8
    """, params, as_dict=True)
    return {"summary": summary, "warehouses": warehouses, "recent": recent}


@frappe.whitelist()
def get_stock_items(search=None, item_group=None, low_stock=0, company=None):
    _require_stock_permission()
    company = company or _get_company()
    conditions = ["i.disabled=0", "i.is_stock_item=1"]
    params = {"company": company}
    if search:
        conditions.append("(i.name LIKE %(search)s OR i.item_name LIKE %(search)s)")
        params["search"] = f"%{search}%"
    if item_group:
        conditions.append("i.item_group=%(item_group)s")
        params["item_group"] = item_group
    having = "HAVING actual_qty <= safety_stock" if cint(low_stock) else ""
    return frappe.db.sql(f"""
        SELECT i.name AS item_code, i.item_name, i.item_group, i.stock_uom,
               i.safety_stock, i.valuation_rate,
               IFNULL(SUM(CASE WHEN w.name IS NOT NULL THEN b.actual_qty ELSE 0 END),0) AS actual_qty,
               IFNULL(SUM(CASE WHEN w.name IS NOT NULL THEN b.reserved_qty ELSE 0 END),0) AS reserved_qty,
               IFNULL(SUM(CASE WHEN w.name IS NOT NULL THEN b.stock_value ELSE 0 END),0) AS stock_value
        FROM `tabItem` i
        LEFT JOIN `tabBin` b ON b.item_code=i.name
        LEFT JOIN `tabWarehouse` w ON w.name=b.warehouse AND w.company=%(company)s
        WHERE {' AND '.join(conditions)}
        GROUP BY i.name, i.item_name, i.item_group, i.stock_uom,
                 i.safety_stock, i.valuation_rate
        {having}
        ORDER BY i.modified DESC LIMIT 500
    """, params, as_dict=True)


@frappe.whitelist(methods=["POST"])
def create_stock_item(item_code, item_name, item_group, stock_uom,
                      valuation_rate=0, safety_stock=0, description=None):
    _require_stock_permission("create")
    if frappe.db.exists("Item", item_code):
        frappe.throw(f"Item {item_code} already exists.")
    doc = frappe.get_doc({
        "doctype": "Item",
        "item_code": cstr(item_code).strip(),
        "item_name": cstr(item_name).strip(),
        "item_group": item_group,
        "stock_uom": stock_uom,
        "is_stock_item": 1,
        "is_sales_item": 1,
        "is_purchase_item": 1,
        "valuation_rate": flt(valuation_rate),
        "safety_stock": flt(safety_stock),
        "description": description or item_name,
    })
    doc.insert()
    return {"name": doc.name, "item_name": doc.item_name}


@frappe.whitelist()
def get_stock_ledger_report(from_date=None, to_date=None, item_code=None,
                            warehouse=None, company=None):
    _require_stock_permission()
    company = company or _get_company()
    conditions = ["sle.company=%(company)s", "sle.is_cancelled=0"]
    params = {"company": company}
    if from_date:
        conditions.append("sle.posting_date >= %(from_date)s")
        params["from_date"] = from_date
    if to_date:
        conditions.append("sle.posting_date <= %(to_date)s")
        params["to_date"] = to_date
    if item_code:
        conditions.append("sle.item_code=%(item_code)s")
        params["item_code"] = item_code
    if warehouse:
        conditions.append("sle.warehouse=%(warehouse)s")
        params["warehouse"] = warehouse
    return frappe.db.sql(f"""
        SELECT sle.posting_date, sle.posting_time, sle.item_code, i.item_name,
               sle.warehouse, sle.actual_qty, sle.qty_after_transaction,
               sle.incoming_rate, sle.valuation_rate, sle.stock_value_difference,
               sle.voucher_type, sle.voucher_no
        FROM `tabStock Ledger Entry` sle
        LEFT JOIN `tabItem` i ON i.name=sle.item_code
        WHERE {' AND '.join(conditions)}
        ORDER BY sle.posting_date DESC, sle.posting_time DESC, sle.creation DESC
        LIMIT 1000
    """, params, as_dict=True)


# ─────────────────────────── REPORTS ───────────────────────────

def _portal_fx(base, target, on_date):
    """Conversion rate base→target (1.0 if same or unavailable)."""
    if not target or target == base:
        return 1.0
    try:
        from erpnext.setup.utils import get_exchange_rate
        return flt(get_exchange_rate(base, target, on_date)) or 1.0
    except Exception:
        return 1.0


@frappe.whitelist()
def get_portal_report(report_type, from_date=None, to_date=None, party=None, currency=None):
    """Return pre-aggregated report data for the portal."""
    try:
        company = _get_company()
        now = frappe.utils.nowdate()
        fd = from_date or frappe.utils.get_first_day(now)
        td = to_date or now
        base_cur = frappe.get_value("Company", company, "default_currency") or "AED"
        cur = currency or base_cur
        rate = _portal_fx(base_cur, cur, td)

        if report_type == "accounts_receivable":
            rows = frappe.db.sql("""
                SELECT si.customer AS party, si.customer_name AS party_name,
                       SUM(si.outstanding_amount) AS outstanding,
                       SUM(si.grand_total) AS billed, COUNT(*) AS invoices
                FROM `tabSales Invoice` si
                WHERE si.docstatus=1 AND si.outstanding_amount > 0
                  AND si.company=%(co)s
                GROUP BY si.customer, si.customer_name
                ORDER BY outstanding DESC LIMIT 100
            """, {"co": company}, as_dict=True)
            total = sum(r.outstanding or 0 for r in rows)
            return {"rows": rows, "total": total, "label": "Accounts Receivable", "columns":
                    [("Customer","party_name"),("Total Billed","billed"),("Outstanding","outstanding"),("Invoices","invoices")]}

        elif report_type == "accounts_payable":
            rows = frappe.db.sql("""
                SELECT pi.supplier AS party, pi.supplier_name AS party_name,
                       SUM(pi.outstanding_amount) AS outstanding,
                       SUM(pi.grand_total) AS billed, COUNT(*) AS invoices
                FROM `tabPurchase Invoice` pi
                WHERE pi.docstatus=1 AND pi.outstanding_amount > 0
                  AND pi.company=%(co)s
                GROUP BY pi.supplier, pi.supplier_name
                ORDER BY outstanding DESC LIMIT 100
            """, {"co": company}, as_dict=True)
            total = sum(r.outstanding or 0 for r in rows)
            return {"rows": rows, "total": total, "label": "Accounts Payable", "columns":
                    [("Supplier","party_name"),("Total Billed","billed"),("Outstanding","outstanding"),("Invoices","invoices")]}

        elif report_type == "sales_summary":
            rows = frappe.db.sql("""
                SELECT DATE_FORMAT(si.posting_date,'%%Y-%%m') AS period,
                       SUM(si.grand_total) AS total,
                       SUM(si.base_total_taxes_and_charges) AS tax,
                       COUNT(*) AS invoices, SUM(si.net_total) AS net
                FROM `tabSales Invoice` si
                WHERE si.docstatus=1 AND si.company=%(co)s
                  AND DATE(si.posting_date) BETWEEN %(fd)s AND %(td)s
                GROUP BY period ORDER BY period ASC
            """, {"co": company, "fd": fd, "td": td}, as_dict=True)
            total = sum(r.total or 0 for r in rows)
            return {"rows": rows, "total": total, "label": "Sales Summary", "columns":
                    [("Period","period"),("Net Sales","net"),("Tax","tax"),("Total","total"),("Invoices","invoices")]}

        elif report_type == "purchase_summary":
            rows = frappe.db.sql("""
                SELECT DATE_FORMAT(pi.posting_date,'%%Y-%%m') AS period,
                       SUM(pi.grand_total) AS total, COUNT(*) AS invoices,
                       SUM(pi.net_total) AS net
                FROM `tabPurchase Invoice` pi
                WHERE pi.docstatus=1 AND pi.company=%(co)s
                  AND DATE(pi.posting_date) BETWEEN %(fd)s AND %(td)s
                GROUP BY period ORDER BY period ASC
            """, {"co": company, "fd": fd, "td": td}, as_dict=True)
            total = sum(r.total or 0 for r in rows)
            return {"rows": rows, "total": total, "label": "Purchase Summary", "columns":
                    [("Period","period"),("Net","net"),("Total","total"),("Invoices","invoices")]}

        elif report_type == "profit_loss":
            income = frappe.db.sql("""
                SELECT SUM(gle.credit - gle.debit) AS amount, a.account_name, a.account_type
                FROM `tabGL Entry` gle
                JOIN `tabAccount` a ON a.name = gle.account
                WHERE gle.company=%(co)s AND a.root_type='Income'
                  AND DATE(gle.posting_date) BETWEEN %(fd)s AND %(td)s
                GROUP BY a.name, a.account_name, a.account_type
                ORDER BY amount DESC
            """, {"co": company, "fd": fd, "td": td}, as_dict=True)
            expense = frappe.db.sql("""
                SELECT SUM(gle.debit - gle.credit) AS amount, a.account_name, a.account_type
                FROM `tabGL Entry` gle
                JOIN `tabAccount` a ON a.name = gle.account
                WHERE gle.company=%(co)s AND a.root_type='Expense'
                  AND DATE(gle.posting_date) BETWEEN %(fd)s AND %(td)s
                GROUP BY a.name, a.account_name, a.account_type
                ORDER BY amount DESC
            """, {"co": company, "fd": fd, "td": td}, as_dict=True)
            if rate != 1.0:
                for r in income: r.amount = flt(r.amount) * rate
                for r in expense: r.amount = flt(r.amount) * rate
            total_income  = sum(r.amount or 0 for r in income)
            total_expense = sum(r.amount or 0 for r in expense)
            return {"income": income, "expense": expense, "total_income": total_income,
                    "total_expense": total_expense, "net_profit": total_income - total_expense,
                    "label": "Profit & Loss", "currency": cur, "rate": rate,
                    "columns": [("Account","account_name"),("Amount","amount")]}

        elif report_type == "expense_claims":
            rows = frappe.db.sql("""
                SELECT ec.name, ec.employee, ec.employee_name, ec.posting_date,
                       ec.total_claimed_amount, ec.total_sanctioned_amount, ec.status
                FROM `tabExpense Claim` ec
                WHERE ec.docstatus < 2 AND ec.company=%(co)s
                  AND DATE(ec.posting_date) BETWEEN %(fd)s AND %(td)s
                ORDER BY ec.posting_date DESC LIMIT 200
            """, {"co": company, "fd": fd, "td": td}, as_dict=True)
            return {"rows": rows, "total": sum(r.total_claimed_amount or 0 for r in rows),
                    "label": "Expense Claims", "columns":
                    [("Employee","employee_name"),("Date","posting_date"),("Claimed","total_claimed_amount"),("Sanctioned","total_sanctioned_amount"),("Status","status")]}

        elif report_type == "payroll_summary":
            rows = frappe.db.sql("""
                SELECT ss.employee, ss.employee_name, ss.start_date, ss.end_date,
                       ss.gross_pay, ss.total_deduction, ss.net_pay, ss.status
                FROM `tabSalary Slip` ss
                WHERE ss.docstatus < 2 AND ss.company=%(co)s
                  AND DATE(ss.start_date) >= %(fd)s AND DATE(ss.end_date) <= %(td)s
                ORDER BY ss.start_date DESC LIMIT 300
            """, {"co": company, "fd": fd, "td": td}, as_dict=True)
            return {"rows": rows, "total": sum(r.net_pay or 0 for r in rows),
                    "label": "Payroll Summary", "columns":
                    [("Employee","employee_name"),("Period","start_date"),("Gross Pay","gross_pay"),("Deductions","total_deduction"),("Net Pay","net_pay"),("Status","status")]}

        elif report_type == "asset_register":
            rows = frappe.db.sql("""
                SELECT a.asset_name, a.asset_category, a.purchase_date,
                       a.gross_purchase_amount,
                       GREATEST(a.gross_purchase_amount - a.value_after_depreciation, 0)
                           AS accumulated_depreciation_amount,
                       a.value_after_depreciation AS net_asset_value, a.status
                FROM `tabAsset` a
                WHERE a.docstatus < 2 AND a.company=%(co)s
                ORDER BY a.purchase_date DESC LIMIT 200
            """, {"co": company}, as_dict=True)
            return {"rows": rows, "total": sum(r.net_asset_value or 0 for r in rows),
                    "label": "Asset Register", "columns":
                    [("Asset","asset_name"),("Category","asset_category"),("Purchase Date","purchase_date"),("Gross Value","gross_purchase_amount"),("Depreciation","accumulated_depreciation_amount"),("Net Value","net_asset_value"),("Status","status")]}

        elif report_type == "trial_balance":
            rows = frappe.db.sql("""
                SELECT a.name AS account, a.account_name AS account_name, a.root_type AS root_type,
                       IFNULL(SUM(g.debit),0) AS dr, IFNULL(SUM(g.credit),0) AS cr,
                       IFNULL(SUM(g.debit - g.credit),0) AS net
                FROM `tabGL Entry` g
                INNER JOIN `tabAccount` a ON a.name = g.account
                WHERE g.company=%(co)s AND g.is_cancelled=0 AND g.posting_date <= %(td)s
                GROUP BY a.name HAVING ABS(net) > 0.005
                ORDER BY a.lft
            """, {"co": company, "td": td}, as_dict=True)
            srows, tdr, tcr = [], 0.0, 0.0
            for r in rows:
                net = flt(r.net) * rate
                d = net if net > 0 else 0.0
                c = -net if net < 0 else 0.0
                tdr += d; tcr += c
                srows.append({"label": r.account_name or r.account, "debit": round(d, 2), "credit": round(c, 2)})
            return {"type": "statement", "mode": "dr_cr", "label": "Trial Balance",
                    "currency": cur, "rate": rate, "as_of": td,
                    "sections": [{"title": "Ledger Balances", "rows": srows,
                                  "total_debit": round(tdr, 2), "total_credit": round(tcr, 2)}],
                    "footer": {"label": "Total", "debit": round(tdr, 2), "credit": round(tcr, 2),
                               "balanced": abs(tdr - tcr) < 1}}

        elif report_type == "balance_sheet":
            def _bs(root_type, sign):
                rs = frappe.db.sql("""
                    SELECT a.account_name AS label,
                           IFNULL(SUM(g.debit - g.credit),0) AS amount
                    FROM `tabGL Entry` g
                    INNER JOIN `tabAccount` a ON a.name = g.account
                    WHERE g.company=%(co)s AND g.is_cancelled=0 AND g.posting_date <= %(td)s
                      AND a.root_type=%(rt)s
                    GROUP BY a.name HAVING ABS(amount) > 0.005
                    ORDER BY a.lft
                """, {"co": company, "td": td, "rt": root_type}, as_dict=True)
                out = [{"label": r.label, "amount": round(flt(r.amount) * sign * rate, 2)} for r in rs]
                return out, round(sum(x["amount"] for x in out), 2)

            assets, a_tot = _bs("Asset", 1)
            liab, l_tot = _bs("Liability", -1)
            equity, e_tot = _bs("Equity", -1)
            prov = flt(frappe.db.sql("""
                SELECT IFNULL(SUM(g.credit - g.debit),0)
                FROM `tabGL Entry` g INNER JOIN `tabAccount` a ON a.name = g.account
                WHERE g.company=%(co)s AND g.is_cancelled=0 AND g.posting_date <= %(td)s
                  AND a.root_type IN ('Income','Expense')
            """, {"co": company, "td": td})[0][0]) * rate
            equity_rows = equity + [{"label": "Profit / (loss) for the period (provisional)",
                                     "amount": round(prov, 2), "italic": True}]
            eq_total = round(e_tot + prov, 2)
            le_total = round(l_tot + eq_total, 2)
            return {"type": "statement", "mode": "amount", "label": "Balance Sheet",
                    "currency": cur, "rate": rate, "as_of": td,
                    "sections": [
                        {"title": "Assets", "rows": assets, "total": a_tot, "total_label": "Total Assets"},
                        {"title": "Liabilities", "rows": liab, "total": l_tot, "total_label": "Total Liabilities"},
                        {"title": "Equity", "rows": equity_rows, "total": eq_total, "total_label": "Total Equity"},
                    ],
                    "footer": {"label": "Total Liabilities + Equity", "amount": le_total,
                               "balanced": abs(a_tot - le_total) < 1, "compare": a_tot}}

        elif report_type == "cash_flow":
            cash_filter = "a.account_type IN ('Bank','Cash') AND a.is_group=0"
            opening = flt(frappe.db.sql(f"""
                SELECT IFNULL(SUM(g.debit - g.credit),0)
                FROM `tabGL Entry` g INNER JOIN `tabAccount` a ON a.name = g.account
                WHERE g.company=%(co)s AND g.is_cancelled=0 AND g.posting_date < %(fd)s AND {cash_filter}
            """, {"co": company, "fd": fd})[0][0]) * rate
            months = frappe.db.sql(f"""
                SELECT DATE_FORMAT(g.posting_date,'%%Y-%%m') AS period,
                       IFNULL(SUM(g.debit),0) AS inflow, IFNULL(SUM(g.credit),0) AS outflow
                FROM `tabGL Entry` g INNER JOIN `tabAccount` a ON a.name = g.account
                WHERE g.company=%(co)s AND g.is_cancelled=0
                  AND g.posting_date BETWEEN %(fd)s AND %(td)s AND {cash_filter}
                GROUP BY period ORDER BY period ASC
            """, {"co": company, "fd": fd, "td": td}, as_dict=True)
            inflow = sum(flt(r.inflow) for r in months) * rate
            outflow = sum(flt(r.outflow) for r in months) * rate
            net = inflow - outflow
            closing = opening + net
            mrows = [{"label": r.period, "amount": round((flt(r.inflow) - flt(r.outflow)) * rate, 2)}
                     for r in months]
            return {"type": "statement", "mode": "amount", "label": "Cash Flow Statement",
                    "currency": cur, "rate": rate, "as_of": f"{fd} → {td}",
                    "sections": [
                        {"title": "Cash Position", "rows": [
                            {"label": "Opening balance", "amount": round(opening, 2)},
                            {"label": "Total cash in", "amount": round(inflow, 2), "col": "#16a34a"},
                            {"label": "Total cash out", "amount": round(-outflow, 2), "col": "#dc2626"},
                            {"label": "Net change", "amount": round(net, 2), "bold": True},
                        ]},
                        {"title": "Monthly Net Movement", "rows": mrows or [{"label": "No movement", "amount": 0}]},
                    ],
                    "footer": {"label": "Closing balance", "amount": round(closing, 2), "bold": True}}

        return {"rows": [], "total": 0, "label": report_type}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), 'Portal: get_portal_report')
        frappe.throw(str(e))


# ─────────────────────────── BANKING ───────────────────────────

@frappe.whitelist()
def get_bank_accounts():
    try:
        return frappe.db.sql("""
            SELECT ba.name, ba.account_name, ba.bank, ba.bank_account_no AS account_no, ba.iban,
                   gl.account_currency AS currency, ba.company, ba.is_company_account,
                   ba.is_default AS is_default_account,
                   gl.name AS gl_account
            FROM `tabBank Account` ba
            LEFT JOIN `tabAccount` gl ON gl.name = ba.account
            WHERE ba.is_company_account = 1
            ORDER BY ba.is_default DESC, ba.account_name ASC
        """, as_dict=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_bank_accounts")
        return []


@frappe.whitelist()
def create_bank_account(account_name, bank, account_no=None, iban=None,
                        currency=None, company=None):
    try:
        if not company:
            company = _get_company()
        if not currency:
            currency = frappe.db.get_value("Company", company, "default_currency") or "AED"

        # Ensure Bank exists
        if bank and not frappe.db.exists("Bank", bank):
            frappe.get_doc({"doctype": "Bank", "bank_name": bank}).insert(ignore_permissions=True)

        gl_account = frappe.db.get_value(
            "Account",
            {"account_name": account_name, "company": company, "account_type": "Bank"},
            "name",
        )
        if not gl_account:
            parent = frappe.db.get_value(
                "Account",
                {"company": company, "root_type": "Asset", "account_type": "Bank", "is_group": 1},
                "name",
            )
            if not parent:
                parent = frappe.db.get_value(
                    "Account",
                    {"company": company, "root_type": "Asset", "is_group": 1, "parent_account": ["is", "set"]},
                    "name",
                    order_by="lft desc",
                )
            if not parent:
                frappe.throw("No bank ledger group found in the company chart of accounts.")
            gl_doc = frappe.get_doc({
                "doctype": "Account",
                "account_name": account_name,
                "parent_account": parent,
                "company": company,
                "account_type": "Bank",
                "account_currency": currency,
            }).insert(ignore_permissions=True)
            gl_account = gl_doc.name

        doc = frappe.get_doc({
            "doctype": "Bank Account",
            "account_name": account_name,
            "bank": bank,
            "account": gl_account,
            "bank_account_no": account_no or "",
            "iban": iban or "",
            "company": company,
            "is_company_account": 1,
        })
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "ok": True}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: create_bank_account")
        frappe.throw(str(e))


@frappe.whitelist()
def get_bank_transactions(bank_account, from_date=None, to_date=None, status=None):
    try:
        cond, vals = "WHERE bt.bank_account = %(ba)s AND bt.docstatus < 2", {"ba": bank_account}
        if from_date:
            cond += " AND bt.date >= %(fd)s"; vals["fd"] = from_date
        if to_date:
            cond += " AND bt.date <= %(td)s"; vals["td"] = to_date
        if status:
            cond += " AND bt.status = %(st)s"; vals["st"] = status
        return frappe.db.sql(f"""
            SELECT bt.name, bt.date, bt.description, bt.deposit, bt.withdrawal,
                   bt.currency, bt.status, bt.reference_number, bt.transaction_id,
                   bt.allocated_amount, bt.unallocated_amount,
                   bt.bank_party_name, bt.party_type, bt.party
            FROM `tabBank Transaction` bt
            {cond}
            ORDER BY bt.date DESC, bt.creation DESC
            LIMIT 500
        """, vals, as_dict=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_bank_transactions")
        return []


@frappe.whitelist()
def import_bank_statement(bank_account, transactions):
    """Create Bank Transaction records from imported statement rows."""
    import json
    try:
        rows = json.loads(transactions) if isinstance(transactions, str) else transactions
        company = frappe.db.get_value("Bank Account", bank_account, "company") or _get_company()
        currency = frappe.db.get_value("Bank Account", bank_account, "currency") or "AED"
        created, skipped = 0, 0
        for r in rows:
            date = r.get("date")
            deposit = flt(r.get("deposit") or 0)
            withdrawal = flt(r.get("withdrawal") or 0)
            description = r.get("description") or ""
            ref = r.get("reference_number") or ""
            if not date:
                skipped += 1
                continue
            # Skip duplicates by reference_number or transaction_id
            if ref and frappe.db.exists("Bank Transaction", {
                "bank_account": bank_account, "reference_number": ref
            }):
                skipped += 1
                continue
            doc = frappe.get_doc({
                "doctype": "Bank Transaction",
                "bank_account": bank_account,
                "date": date,
                "deposit": deposit,
                "withdrawal": withdrawal,
                "currency": currency,
                "description": description,
                "reference_number": ref,
                "bank_party_name": r.get("party_name") or "",
                "company": company,
                "status": "Pending",
            })
            doc.insert(ignore_permissions=True)
            doc.submit()
            created += 1
        frappe.db.commit()
        return {"created": created, "skipped": skipped, "ok": True}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: import_bank_statement")
        frappe.throw(str(e))


@frappe.whitelist()
def get_unreconciled_transactions(bank_account, from_date=None, to_date=None):
    """Return pending/unreconciled bank transactions and unmatched payment entries."""
    try:
        cond, vals = "WHERE bt.bank_account=%(ba)s AND bt.docstatus=1 AND bt.status IN ('Pending','Unreconciled')", {"ba": bank_account}
        if from_date:
            cond += " AND bt.date >= %(fd)s"; vals["fd"] = from_date
        if to_date:
            cond += " AND bt.date <= %(td)s"; vals["td"] = to_date
        bank_txns = frappe.db.sql(f"""
            SELECT bt.name, bt.date, bt.description, bt.deposit, bt.withdrawal,
                   bt.currency, bt.status, bt.reference_number, bt.unallocated_amount,
                   bt.bank_party_name
            FROM `tabBank Transaction` bt {cond}
            ORDER BY bt.date DESC LIMIT 200
        """, vals, as_dict=True)

        company = frappe.db.get_value("Bank Account", bank_account, "company") or _get_company()
        gl_account = frappe.db.get_value("Bank Account", bank_account, "account")
        pe_cond, pe_vals = "WHERE pe.docstatus=1 AND pe.company=%(co)s AND pe.clearance_date IS NULL", {"co": company}
        if gl_account:
            pe_cond += " AND (pe.paid_from=%(gl)s OR pe.paid_to=%(gl)s)"; pe_vals["gl"] = gl_account
        if from_date:
            pe_cond += " AND pe.posting_date >= %(fd)s"; pe_vals["fd"] = from_date
        if to_date:
            pe_cond += " AND pe.posting_date <= %(td)s"; pe_vals["td"] = to_date
        payments = frappe.db.sql(f"""
            SELECT pe.name, pe.posting_date AS date, pe.payment_type, pe.party_type,
                   pe.party, pe.party_name, pe.paid_amount,
                   pe.paid_to_account_currency AS currency,
                   pe.reference_no, pe.mode_of_payment
            FROM `tabPayment Entry` pe {pe_cond}
            ORDER BY pe.posting_date DESC LIMIT 200
        """, pe_vals, as_dict=True)
        return {"transactions": bank_txns, "payments": payments}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_unreconciled_transactions")
        return {"transactions": [], "payments": []}


@frappe.whitelist()
def reconcile_bank_transaction(bank_transaction, payment_entry, amount=None):
    """Link a bank transaction to a payment entry and mark as reconciled."""
    try:
        bt = frappe.get_doc("Bank Transaction", bank_transaction)
        allocated = flt(amount) if amount else (flt(bt.deposit) or flt(bt.withdrawal))

        # Add payment entry to bank transaction's payments child table
        found = False
        for row in bt.get("payment_entries", []):
            if row.payment_document == "Payment Entry" and row.payment_entry == payment_entry:
                found = True
                break
        if not found:
            bt.append("payment_entries", {
                "payment_document": "Payment Entry",
                "payment_entry": payment_entry,
                "allocated_amount": allocated,
            })

        bt.flags.ignore_permissions = True
        bt.save()

        # Update clearance date on the payment entry
        frappe.db.set_value("Payment Entry", payment_entry, "clearance_date", bt.date)
        frappe.db.commit()
        return {"ok": True, "status": bt.status}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: reconcile_bank_transaction")
        frappe.throw(str(e))


@frappe.whitelist()
def get_bank_summary(bank_account):
    """Return balance summary for a bank account."""
    try:
        result = frappe.db.sql("""
            SELECT
                SUM(bt.deposit)    AS total_deposits,
                SUM(bt.withdrawal) AS total_withdrawals,
                COUNT(*)           AS total_txns,
                SUM(CASE WHEN bt.status IN ('Pending','Unreconciled') THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN bt.status = 'Reconciled' THEN 1 ELSE 0 END) AS reconciled_count
            FROM `tabBank Transaction` bt
            WHERE bt.bank_account = %(ba)s AND bt.docstatus = 1
        """, {"ba": bank_account}, as_dict=True)
        return result[0] if result else {}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_bank_summary")
        return {}


# ─────────────────────────── DASHBOARD OVERVIEW ───────────────────────────

@frappe.whitelist()
def get_portal_insights():
    """Compact analytics used by the customer and invoice module headers."""
    company = _get_company()
    today = frappe.utils.today()
    month_start = frappe.utils.get_first_day(today)

    customer = frappe.db.sql("""
        SELECT
          (SELECT COUNT(*) FROM `tabCustomer` WHERE disabled=0) AS total,
          (SELECT COUNT(DISTINCT customer) FROM `tabSales Invoice`
             WHERE docstatus=1 AND company=%(co)s) AS billed,
          (SELECT COUNT(*) FROM `tabCustomer`
             WHERE disabled=0 AND DATE(creation) >= %(month_start)s) AS new_this_month,
          (SELECT IFNULL(SUM(grand_total),0) FROM `tabSales Invoice`
             WHERE docstatus=1 AND company=%(co)s) AS lifetime_value
    """, {"co": company, "month_start": month_start}, as_dict=True)[0]

    invoices = frappe.db.sql("""
        SELECT COUNT(*) AS total_count,
               IFNULL(SUM(grand_total),0) AS total_billed,
               IFNULL(SUM(outstanding_amount),0) AS outstanding,
               SUM(CASE WHEN outstanding_amount=0 THEN 1 ELSE 0 END) AS paid_count,
               SUM(CASE WHEN outstanding_amount>0 AND due_date < %(today)s THEN 1 ELSE 0 END) AS overdue_count,
               IFNULL(SUM(CASE WHEN outstanding_amount>0 AND due_date < %(today)s
                           THEN outstanding_amount ELSE 0 END),0) AS overdue_amount
        FROM `tabSales Invoice`
        WHERE docstatus=1 AND company=%(co)s
    """, {"co": company, "today": today}, as_dict=True)[0]

    monthly = frappe.db.sql("""
        SELECT DATE_FORMAT(posting_date,'%%Y-%%m') AS period,
               SUM(grand_total) AS total, COUNT(*) AS count
        FROM `tabSales Invoice`
        WHERE docstatus=1 AND company=%(co)s
          AND posting_date >= DATE_SUB(%(today)s, INTERVAL 5 MONTH)
        GROUP BY period ORDER BY period
    """, {"co": company, "today": today}, as_dict=True)

    top_customers = frappe.db.sql("""
        SELECT customer_name, SUM(grand_total) AS total, COUNT(*) AS invoices
        FROM `tabSales Invoice`
        WHERE docstatus=1 AND company=%(co)s
        GROUP BY customer, customer_name ORDER BY total DESC LIMIT 5
    """, {"co": company}, as_dict=True)

    return {
        "customers": customer,
        "invoices": invoices,
        "monthly_sales": monthly,
        "top_customers": top_customers,
    }


# UAE Federal Tax Authority VAT registration thresholds (AED)
VAT_VOLUNTARY_THRESHOLD = 187500
VAT_MANDATORY_THRESHOLD = 375000


@frappe.whitelist()
def get_vat_registration_status():
    """Compute trailing 12-month taxable turnover and flag VAT registration
    obligations against the UAE FTA thresholds.

    - Turnover >= AED 187,500  → eligible for *voluntary* registration.
    - Turnover >= AED 375,000  → *mandatory* registration required within 30 days.

    Already-registered companies (a TRN is stored on Company.tax_id) are reported
    as registered so the portal can suppress the prompts.
    """
    try:
        company = _get_company()
        today = frappe.utils.today()
        start = frappe.utils.add_days(today, -365)

        turnover = frappe.db.sql("""
            SELECT IFNULL(SUM(base_grand_total), 0)
            FROM `tabSales Invoice`
            WHERE docstatus=1 AND company=%(co)s AND posting_date >= %(start)s
        """, {"co": company, "start": start})[0][0]
        turnover = flt(turnover)

        trn = frappe.get_value("Company", company, "tax_id") if company else None
        registered = bool(trn)

        if turnover >= VAT_MANDATORY_THRESHOLD:
            level = "mandatory"
        elif turnover >= VAT_VOLUNTARY_THRESHOLD:
            level = "voluntary"
        else:
            level = "none"

        return {
            "turnover_12m": turnover,
            "registered": registered,
            "trn": trn or "",
            "level": level,
            "voluntary_threshold": VAT_VOLUNTARY_THRESHOLD,
            "mandatory_threshold": VAT_MANDATORY_THRESHOLD,
            "currency": frappe.get_value("Company", company, "default_currency") or "AED" if company else "AED",
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_vat_registration_status")
        return {"turnover_12m": 0, "registered": False, "trn": "", "level": "none",
                "voluntary_threshold": VAT_VOLUNTARY_THRESHOLD,
                "mandatory_threshold": VAT_MANDATORY_THRESHOLD, "currency": "AED"}

@frappe.whitelist()
def get_dashboard_overview(from_date=None, to_date=None):
    """Rich dashboard data: KPIs, monthly chart, top customers, overdue, pipeline."""
    try:
        company = _get_company()
        today   = frappe.utils.today()
        yr      = today[:4]
        # Current month bounds
        fd = from_date or frappe.utils.get_first_day(today)
        td = to_date   or today

        # ── KPIs ──
        kpis = frappe.db.sql("""
            SELECT
              (SELECT IFNULL(SUM(grand_total),0) FROM `tabSales Invoice`    WHERE docstatus=1 AND company=%(co)s) AS total_sales,
              (SELECT IFNULL(SUM(grand_total),0) FROM `tabPurchase Invoice` WHERE docstatus=1 AND company=%(co)s) AS total_purchase,
              (SELECT IFNULL(SUM(outstanding_amount),0) FROM `tabSales Invoice` WHERE docstatus=1 AND outstanding_amount>0 AND company=%(co)s) AS accounts_receivable,
              (SELECT IFNULL(SUM(outstanding_amount),0) FROM `tabPurchase Invoice` WHERE docstatus=1 AND outstanding_amount>0 AND company=%(co)s) AS accounts_payable,
              (SELECT COUNT(*) FROM `tabCustomer` WHERE disabled=0) AS customers,
              (SELECT COUNT(*) FROM `tabSupplier` WHERE disabled=0) AS suppliers,
              (SELECT COUNT(*) FROM `tabEmployee` WHERE status='Active' AND company=%(co)s) AS employees,
              (SELECT COUNT(*) FROM `tabSales Invoice` WHERE docstatus=1 AND outstanding_amount>0 AND due_date < %(today)s AND company=%(co)s) AS overdue_count,
              (SELECT IFNULL(SUM(outstanding_amount),0) FROM `tabSales Invoice` WHERE docstatus=1 AND outstanding_amount>0 AND due_date < %(today)s AND company=%(co)s) AS overdue_amount,
              (SELECT COUNT(*) FROM `tabLead` WHERE status NOT IN ('Converted','Do Not Contact')) AS open_leads,
              (SELECT COUNT(*) FROM `tabOpportunity` WHERE docstatus=0 AND sales_stage NOT IN ('Closed Won','Closed Lost')) AS open_opps,
              (SELECT IFNULL(SUM(opportunity_amount),0) FROM `tabOpportunity` WHERE docstatus=0 AND sales_stage NOT IN ('Closed Won','Closed Lost')) AS pipeline_value,
              (SELECT IFNULL(SUM(grand_total),0) FROM `tabSales Invoice` WHERE docstatus=1 AND company=%(co)s AND DATE(posting_date) BETWEEN %(fd)s AND %(td)s) AS this_period_sales,
              (SELECT IFNULL(SUM(grand_total),0) FROM `tabPurchase Invoice` WHERE docstatus=1 AND company=%(co)s AND DATE(posting_date) BETWEEN %(fd)s AND %(td)s) AS this_period_purchase
        """, {"co": company, "today": today, "fd": fd, "td": td}, as_dict=True)
        kpi = kpis[0] if kpis else {}

        # ── Monthly revenue vs expenses (12 calendar months of current year) ──
        sales_rows = frappe.db.sql("""
            SELECT DATE_FORMAT(posting_date,'%%Y-%%m') AS period,
                   SUM(grand_total) AS sales, COUNT(*) AS invoice_count
            FROM `tabSales Invoice`
            WHERE docstatus=1 AND company=%(co)s AND YEAR(posting_date)=%(yr)s
            GROUP BY period
        """, {"co": company, "yr": yr}, as_dict=True)

        purchase_rows = frappe.db.sql("""
            SELECT DATE_FORMAT(posting_date,'%%Y-%%m') AS period, SUM(grand_total) AS purchases
            FROM `tabPurchase Invoice`
            WHERE docstatus=1 AND company=%(co)s AND YEAR(posting_date)=%(yr)s
            GROUP BY period
        """, {"co": company, "yr": yr}, as_dict=True)

        sales_map = {r.period: flt(r.sales) for r in sales_rows}
        inv_map   = {r.period: int(r.invoice_count) for r in sales_rows}
        pur_map   = {r.period: flt(r.purchases) for r in purchase_rows}

        monthly = []
        for mth in range(1, 13):
            period = "%s-%02d" % (yr, mth)
            s = sales_map.get(period, 0.0)
            p = pur_map.get(period, 0.0)
            monthly.append({
                "period": period,
                "sales": s,
                "purchases": p,
                "net": s - p,
                "invoice_count": inv_map.get(period, 0),
            })

        # ── Top customers ──
        top_customers = frappe.db.sql("""
            SELECT customer AS name, customer_name, SUM(grand_total) AS total, COUNT(*) AS invoices
            FROM `tabSales Invoice`
            WHERE docstatus=1 AND company=%(co)s AND YEAR(posting_date)=%(yr)s
            GROUP BY customer, customer_name ORDER BY total DESC LIMIT 5
        """, {"co": company, "yr": yr}, as_dict=True)

        # ── Overdue invoices ──
        overdue = frappe.db.sql("""
            SELECT name, customer_name, due_date, outstanding_amount, grand_total
            FROM `tabSales Invoice`
            WHERE docstatus=1 AND outstanding_amount>0 AND due_date < %(today)s AND company=%(co)s
            ORDER BY due_date ASC LIMIT 8
        """, {"co": company, "today": today}, as_dict=True)

        # ── Recent sales invoices ──
        recent = frappe.db.sql("""
            SELECT name, customer_name, posting_date, grand_total, outstanding_amount, status
            FROM `tabSales Invoice`
            WHERE docstatus=1 AND company=%(co)s
            ORDER BY posting_date DESC, creation DESC LIMIT 6
        """, {"co": company}, as_dict=True)

        # ── Purchase by supplier top 5 ──
        top_suppliers = frappe.db.sql("""
            SELECT supplier AS name, supplier_name, SUM(grand_total) AS total
            FROM `tabPurchase Invoice`
            WHERE docstatus=1 AND company=%(co)s AND YEAR(posting_date)=%(yr)s
            GROUP BY supplier, supplier_name ORDER BY total DESC LIMIT 5
        """, {"co": company, "yr": yr}, as_dict=True)

        # ── Financial analytics: P&L, bank balances, cash flow, ratios ──
        pnl, bank_balances, cash_flow, ratios = {}, [], {}, {}
        try:
            fy_start = yr + "-01-01"

            # Profit & Loss (year-to-date) grouped by account root_type
            pl_rows = frappe.db.sql("""
                SELECT a.root_type AS root_type,
                       IFNULL(a.account_type,'') AS account_type,
                       SUM(g.credit - g.debit) AS credit_net,
                       SUM(g.debit - g.credit) AS debit_net
                FROM `tabGL Entry` g
                INNER JOIN `tabAccount` a ON a.name = g.account
                WHERE g.company=%(co)s AND g.is_cancelled=0
                  AND g.posting_date BETWEEN %(fd)s AND %(td)s
                GROUP BY a.root_type, a.account_type
            """, {"co": company, "fd": fy_start, "td": today}, as_dict=True)

            income  = sum(flt(r.credit_net) for r in pl_rows if r.root_type == "Income")
            expense = sum(flt(r.debit_net)  for r in pl_rows if r.root_type == "Expense")
            cogs    = sum(flt(r.debit_net)  for r in pl_rows if r.root_type == "Expense" and r.account_type == "Cost of Goods Sold")
            gross_profit = income - cogs
            net_profit   = income - expense
            pnl = {
                "from_date": fy_start, "to_date": today,
                "income": income, "expense": expense, "cogs": cogs,
                "gross_profit": gross_profit, "net_profit": net_profit,
            }

            # Bank & cash balances (cumulative to date)
            bank_balances = frappe.db.sql("""
                SELECT a.name AS account, a.account_name AS account_name,
                       IFNULL(a.account_type,'') AS account_type,
                       SUM(g.debit - g.credit) AS balance
                FROM `tabGL Entry` g
                INNER JOIN `tabAccount` a ON a.name = g.account
                WHERE g.company=%(co)s AND g.is_cancelled=0 AND a.is_group=0
                  AND a.account_type IN ('Bank','Cash') AND g.posting_date <= %(td)s
                GROUP BY a.name HAVING ABS(balance) > 0.005
                ORDER BY balance DESC
            """, {"co": company, "td": today}, as_dict=True)

            # Cash flow — opening (before period) vs closing (to date)
            cf = frappe.db.sql("""
                SELECT
                  IFNULL(SUM(CASE WHEN g.posting_date <  %(fd)s THEN g.debit - g.credit ELSE 0 END),0) AS opening,
                  IFNULL(SUM(CASE WHEN g.posting_date <= %(td)s THEN g.debit - g.credit ELSE 0 END),0) AS closing,
                  IFNULL(SUM(CASE WHEN g.posting_date BETWEEN %(fd)s AND %(td)s THEN g.debit  ELSE 0 END),0) AS inflow,
                  IFNULL(SUM(CASE WHEN g.posting_date BETWEEN %(fd)s AND %(td)s THEN g.credit ELSE 0 END),0) AS outflow
                FROM `tabGL Entry` g
                INNER JOIN `tabAccount` a ON a.name = g.account
                WHERE g.company=%(co)s AND g.is_cancelled=0 AND a.account_type IN ('Bank','Cash')
            """, {"co": company, "fd": fy_start, "td": today}, as_dict=True)
            c = cf[0] if cf else {}
            cash_flow = {
                "from_date": fy_start, "to_date": today,
                "opening": flt(c.get("opening")), "closing": flt(c.get("closing")),
                "inflow": flt(c.get("inflow")), "outflow": flt(c.get("outflow")),
                "net_change": flt(c.get("closing")) - flt(c.get("opening")),
            }

            # Balance-sheet aggregates for ratios (cumulative to date)
            bs_rows = frappe.db.sql("""
                SELECT a.root_type AS root_type, IFNULL(a.account_type,'') AS account_type,
                       SUM(g.debit - g.credit) AS debit_net,
                       SUM(g.credit - g.debit) AS credit_net
                FROM `tabGL Entry` g
                INNER JOIN `tabAccount` a ON a.name = g.account
                WHERE g.company=%(co)s AND g.is_cancelled=0 AND g.posting_date <= %(td)s
                GROUP BY a.root_type, a.account_type
            """, {"co": company, "td": today}, as_dict=True)

            total_assets = sum(flt(r.debit_net)  for r in bs_rows if r.root_type == "Asset")
            total_liab   = sum(flt(r.credit_net) for r in bs_rows if r.root_type == "Liability")
            total_equity = sum(flt(r.credit_net) for r in bs_rows if r.root_type == "Equity")
            stock_val    = sum(flt(r.debit_net)  for r in bs_rows if r.account_type == "Stock")
            current_assets = sum(flt(r.debit_net)  for r in bs_rows if r.root_type == "Asset" and r.account_type in ("Bank", "Cash", "Receivable", "Stock"))
            current_liab   = sum(flt(r.credit_net) for r in bs_rows if r.root_type == "Liability" and r.account_type in ("Payable", "Tax", ""))
            ar = flt(kpi.get("accounts_receivable"))
            ap = flt(kpi.get("accounts_payable"))

            def _ratio(a, b):
                return round(flt(a) / flt(b), 2) if flt(b) else None

            ratios = {
                "current_ratio":  _ratio(current_assets, current_liab),
                "quick_ratio":    _ratio(current_assets - stock_val, current_liab),
                "debt_to_equity": _ratio(total_liab, total_equity),
                "ar_to_ap":       _ratio(ar, ap),
                "gross_margin":   round(gross_profit / income * 100, 1) if income else None,
                "net_margin":     round(net_profit / income * 100, 1) if income else None,
                "total_assets": total_assets, "total_liabilities": total_liab,
                "total_equity": total_equity, "current_assets": current_assets,
                "current_liabilities": current_liab,
            }
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Portal: dashboard financial analytics")

        return {
            "kpi": kpi, "monthly": monthly,
            "top_customers": top_customers, "top_suppliers": top_suppliers,
            "overdue": overdue, "recent": recent, "company": company,
            "pnl": pnl, "bank_balances": bank_balances,
            "cash_flow": cash_flow, "ratios": ratios,
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_dashboard_overview")
        return {"kpi": {}, "monthly": [], "top_customers": [], "overdue": [], "recent": [], "company": "",
                "pnl": {}, "bank_balances": [], "cash_flow": {}, "ratios": {}}


# ── UAE VAT 201 Return ───────────────────────────────────────────
@frappe.whitelist()
def get_vat_201(from_date=None, to_date=None, company=None):
    """Compute a UAE FTA VAT 201 return for the period from invoice data."""
    try:
        if not company:
            company = _get_company()
        today = frappe.utils.today()
        fd = from_date or frappe.utils.get_first_day(today)
        td = to_date or frappe.utils.get_last_day(today)
        currency = frappe.get_value("Company", company, "default_currency") or "AED"
        RATE = 0.05
        EMIRATES = ["Abu Dhabi", "Dubai", "Sharjah", "Ajman",
                    "Umm Al Quwain", "Ras Al Khaimah", "Fujairah"]
        params = {"co": company, "fd": fd, "td": td}

        # Box 1 — standard-rated supplies per emirate (taxable amount)
        rows = frappe.db.sql("""
            SELECT IFNULL(si.vat_emirate,'') AS emirate,
                   IFNULL(SUM(sii.base_net_amount),0) AS amount
            FROM `tabSales Invoice Item` sii
            INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
            WHERE si.docstatus=1 AND si.company=%(co)s
              AND si.posting_date BETWEEN %(fd)s AND %(td)s
              AND IFNULL(sii.is_zero_rated,0)=0 AND IFNULL(sii.is_exempt,0)=0
            GROUP BY si.vat_emirate
        """, params, as_dict=True)
        emap = {(r.emirate or "").strip(): flt(r.amount) for r in rows}
        std_rows = []
        for em in EMIRATES:
            amt = emap.pop(em, 0.0)
            std_rows.append({"emirate": em, "amount": amt, "vat": round(amt * RATE, 2)})
        other = sum(emap.values())
        if round(other, 2):
            std_rows.append({"emirate": "Other / Unspecified", "amount": other,
                             "vat": round(other * RATE, 2)})
        std_amount = sum(r["amount"] for r in std_rows)
        std_vat = sum(r["vat"] for r in std_rows)

        def _sum_si_items(cond):
            return flt(frappe.db.sql(f"""
                SELECT IFNULL(SUM(sii.base_net_amount),0)
                FROM `tabSales Invoice Item` sii
                INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
                WHERE si.docstatus=1 AND si.company=%(co)s
                  AND si.posting_date BETWEEN %(fd)s AND %(td)s AND {cond}
            """, params)[0][0])

        zero_rated = _sum_si_items("IFNULL(sii.is_zero_rated,0)=1")
        exempt = _sum_si_items("IFNULL(sii.is_exempt,0)=1 AND IFNULL(sii.is_zero_rated,0)=0")

        # Inputs — standard-rated expenses (Box 9) and reverse charge (Box 10)
        std_in = frappe.db.sql("""
            SELECT IFNULL(SUM(base_net_total),0) AS amount,
                   IFNULL(SUM(recoverable_standard_rated_expenses),0) AS rec,
                   IFNULL(SUM(base_total_taxes_and_charges),0) AS tax
            FROM `tabPurchase Invoice`
            WHERE docstatus=1 AND company=%(co)s
              AND posting_date BETWEEN %(fd)s AND %(td)s
              AND IFNULL(reverse_charge,'N')!='Y'
        """, params, as_dict=True)[0]
        in_amount = flt(std_in.amount)
        in_vat = flt(std_in.rec) or flt(std_in.tax)

        rc = frappe.db.sql("""
            SELECT IFNULL(SUM(base_net_total),0) AS amount,
                   IFNULL(SUM(recoverable_reverse_charge),0) AS rec
            FROM `tabPurchase Invoice`
            WHERE docstatus=1 AND company=%(co)s
              AND posting_date BETWEEN %(fd)s AND %(td)s
              AND IFNULL(reverse_charge,'N')='Y'
        """, params, as_dict=True)[0]
        rc_amount, rc_vat = flt(rc.amount), flt(rc.rec)

        output_amount = std_amount + zero_rated + exempt
        output_vat = std_vat
        input_amount = in_amount + rc_amount
        input_vat = in_vat + rc_vat
        net = round(output_vat - input_vat, 2)

        return {
            "company": company, "currency": currency,
            "from_date": fd, "to_date": td,
            "standard_rated": std_rows,
            "sales": {
                "standard_amount": round(std_amount, 2), "standard_vat": round(std_vat, 2),
                "tourist_refunds_vat": 0.0,
                "reverse_charge_amount": 0.0, "reverse_charge_vat": 0.0,
                "zero_rated": round(zero_rated, 2), "exempt": round(exempt, 2),
                "imports_amount": 0.0, "imports_vat": 0.0,
                "import_adjust_amount": 0.0, "import_adjust_vat": 0.0,
                "total_amount": round(output_amount, 2), "total_vat": round(output_vat, 2),
            },
            "expenses": {
                "standard_amount": round(in_amount, 2), "standard_vat": round(in_vat, 2),
                "reverse_charge_amount": round(rc_amount, 2), "reverse_charge_vat": round(rc_vat, 2),
                "total_amount": round(input_amount, 2), "total_vat": round(input_vat, 2),
            },
            "net": {
                "output_vat": round(output_vat, 2), "input_vat": round(input_vat, 2),
                "payable": round(net, 2), "is_refund": net < 0,
            },
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_vat_201")
        frappe.throw(str(e))


# ── UAE Corporate Tax Return ─────────────────────────────────────
@frappe.whitelist()
def get_corporate_tax(from_date=None, to_date=None, company=None, adjustments=None):
    """Compute a detailed UAE Corporate Tax return for the period from the ledger.

    Rules (Federal Decree-Law No. 47 of 2022):
      • 0% on taxable income up to AED 375,000; 9% above.
      • Small Business Relief available when revenue <= AED 3,000,000 (elective).
    """
    try:
        if not company:
            company = _get_company()
        today = frappe.utils.today()
        yr = today[:4]
        fd = from_date or (yr + "-01-01")
        td = to_date or (yr + "-12-31")
        currency = frappe.get_value("Company", company, "default_currency") or "AED"
        THRESHOLD = 375000.0          # 0% band (Art. 3)
        RATE = 0.09                   # standard CT rate
        SBR_CAP = 3000000.0           # Small Business Relief revenue cap (Art. 21)
        LOSS_CAP_PCT = 0.75           # tax loss offset cap (Art. 37)
        params = {"co": company, "fd": fd, "td": td}

        adj = adjustments
        if isinstance(adj, str):
            adj = json.loads(adj or "{}")
        adj = adj or {}

        def a(key):
            try:
                return flt(adj.get(key) or 0)
            except Exception:
                return 0.0

        def ab(key):
            v = adj.get(key)
            return str(v).lower() in ("1", "true", "yes", "on") if v is not None else False

        # ── Accounting income from P&L accounts over the period (GL Entry) ──
        pl = frappe.db.sql("""
            SELECT a.root_type AS root_type,
                   IFNULL(SUM(g.credit - g.debit),0) AS credit_net,
                   IFNULL(SUM(g.debit - g.credit),0) AS debit_net
            FROM `tabGL Entry` g
            INNER JOIN `tabAccount` a ON a.name = g.account
            WHERE g.company=%(co)s AND g.is_cancelled=0
              AND g.posting_date BETWEEN %(fd)s AND %(td)s
              AND a.root_type IN ('Income','Expense')
            GROUP BY a.root_type
        """, params, as_dict=True)
        income = sum(flt(r.credit_net) for r in pl if r.root_type == "Income")
        expenses = sum(flt(r.debit_net) for r in pl if r.root_type == "Expense")

        revenue = flt(frappe.db.sql("""
            SELECT IFNULL(SUM(base_net_total),0) FROM `tabSales Invoice`
            WHERE docstatus=1 AND company=%(co)s AND posting_date BETWEEN %(fd)s AND %(td)s
        """, params)[0][0]) or income

        accounting_income = income - expenses

        # ── Add-backs (non-deductible expenses) ──
        addbacks = {
            "entertainment_50": a("entertainment_50"),       # 50% client entertainment (Art. 32)
            "fines_penalties": a("fines_penalties"),         # Art. 33
            "donations_non_qualifying": a("donations_non_qualifying"),
            "owner_related_excess": a("owner_related_excess"),  # excess related-party remuneration
            "depreciation_disallowed": a("depreciation_disallowed"),
            "provisions_general": a("provisions_general"),
            "interest_disallowed": a("interest_disallowed"), # EBITDA / AED 12M limitation (Art. 30)
            "other_addbacks": a("other_addbacks"),
        }
        total_addbacks = round(sum(addbacks.values()), 2)

        # ── Deductions / exempt income (subtractions) ──
        deductions = {
            "exempt_dividends": a("exempt_dividends"),           # Art. 22
            "participation_exemption": a("participation_exemption"),  # Art. 23
            "foreign_pe_exempt": a("foreign_pe_exempt"),         # Art. 24
            "other_deductions": a("other_deductions"),
        }
        total_deductions = round(sum(deductions.values()), 2)

        income_before_relief = accounting_income + total_addbacks - total_deductions

        # ── Reliefs ──
        elect_sbr = ab("elect_sbr")
        sbr_eligible = revenue <= SBR_CAP
        sbr_applied = elect_sbr and sbr_eligible

        prior_year_losses = a("prior_year_losses")
        loss_relief = 0.0
        income_after_losses = income_before_relief
        if not sbr_applied and income_before_relief > 0 and prior_year_losses > 0:
            loss_relief = round(min(prior_year_losses, income_before_relief * LOSS_CAP_PCT), 2)
            income_after_losses = income_before_relief - loss_relief

        taxable_income = 0.0 if sbr_applied else max(0.0, income_after_losses)

        # ── Tax computation ──
        qfzp = ab("qfzp")
        qualifying_income = a("qualifying_income")
        if qfzp:
            # QFZP: 0% on qualifying income, 9% on the remainder (no 375k band)
            zero_band = min(qualifying_income, taxable_income)
            taxed_amount = max(0.0, taxable_income - zero_band)
        else:
            zero_band = min(taxable_income, THRESHOLD)
            taxed_amount = max(0.0, taxable_income - THRESHOLD)
        tax_before_credits = round(taxed_amount * RATE, 2)

        # ── Tax credits ──
        foreign_tax_credit = a("foreign_tax_credit")
        withholding_credit = a("withholding_credit")
        total_credits = round(foreign_tax_credit + withholding_credit, 2)
        net_tax_payable = round(max(0.0, tax_before_credits - total_credits), 2)

        carryforward_loss = 0.0
        if income_before_relief < 0:
            carryforward_loss = round(abs(income_before_relief), 2)

        effective_rate = round((net_tax_payable / taxable_income * 100), 2) if taxable_income else 0.0

        return {
            "company": company, "currency": currency,
            "from_date": fd, "to_date": td,
            "trn": frappe.get_value("Company", company, "tax_id") or "",
            "revenue": round(revenue, 2),
            "income": round(income, 2),
            "expenses": round(expenses, 2),
            "accounting_income": round(accounting_income, 2),
            "addbacks": {k: round(v, 2) for k, v in addbacks.items()},
            "total_addbacks": total_addbacks,
            "deductions": {k: round(v, 2) for k, v in deductions.items()},
            "total_deductions": total_deductions,
            "income_before_relief": round(income_before_relief, 2),
            "elect_sbr": elect_sbr, "sbr_eligible": sbr_eligible, "sbr_applied": sbr_applied,
            "sbr_cap": SBR_CAP,
            "prior_year_losses": round(prior_year_losses, 2),
            "loss_relief": loss_relief, "loss_cap_pct": LOSS_CAP_PCT,
            "taxable_income": round(taxable_income, 2),
            "qfzp": qfzp, "qualifying_income": round(qualifying_income, 2),
            "zero_band": round(zero_band, 2), "threshold": THRESHOLD,
            "taxed_amount": round(taxed_amount, 2), "rate": RATE,
            "tax_before_credits": tax_before_credits,
            "foreign_tax_credit": round(foreign_tax_credit, 2),
            "withholding_credit": round(withholding_credit, 2),
            "total_credits": total_credits,
            "net_tax_payable": net_tax_payable,
            "effective_rate": effective_rate,
            "is_loss": income_before_relief < 0,
            "carryforward_loss": carryforward_loss,
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_corporate_tax")
        frappe.throw(str(e))


# ── Accountant: Journal Entries & Chart of Accounts ──────────────
@frappe.whitelist()
def get_journal_entries(from_date=None, to_date=None, company=None):
    """List Journal Entries for the period."""
    try:
        if not company:
            company = _get_company()
        now = frappe.utils.nowdate()
        fd = from_date or frappe.utils.get_first_day(now)
        td = to_date or now
        rows = frappe.db.sql("""
            SELECT je.name, je.posting_date, je.voucher_type, je.total_debit AS amount,
                   je.user_remark, je.docstatus,
                   GROUP_CONCAT(DISTINCT jea.account ORDER BY jea.idx SEPARATOR ', ') AS accounts
            FROM `tabJournal Entry` je
            LEFT JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
            WHERE je.company=%(co)s AND je.docstatus < 2
              AND je.posting_date BETWEEN %(fd)s AND %(td)s
            GROUP BY je.name
            ORDER BY je.posting_date DESC, je.creation DESC LIMIT 200
        """, {"co": company, "fd": fd, "td": td}, as_dict=True)
        for r in rows:
            r["status"] = "Submitted" if r.docstatus == 1 else "Draft"
        return {"rows": rows, "total": sum(flt(r.amount) for r in rows),
                "currency": frappe.get_value("Company", company, "default_currency") or "AED"}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_journal_entries")
        frappe.throw(str(e))


@frappe.whitelist()
def get_chart_of_accounts(company=None):
    """Return the chart of accounts (ordered tree) with current balances."""
    try:
        if not company:
            company = _get_company()
        currency = frappe.get_value("Company", company, "default_currency") or "AED"
        accounts = frappe.get_all(
            "Account",
            filters={"company": company},
            fields=["name", "account_name", "parent_account", "is_group",
                    "root_type", "account_type", "account_number", "lft", "rgt"],
            order_by="lft asc",
        )
        bal = frappe.db.sql("""
            SELECT account, IFNULL(SUM(debit - credit),0) AS net
            FROM `tabGL Entry`
            WHERE company=%(co)s AND is_cancelled=0
            GROUP BY account
        """, {"co": company}, as_dict=True)
        bmap = {b.account: flt(b.net) for b in bal}
        # roll up balances to group accounts via lft/rgt nesting
        leaves = [a for a in accounts if not a.is_group]
        for a in accounts:
            if a.is_group:
                a["balance"] = round(sum(bmap.get(l.name, 0.0) for l in leaves
                                         if l.lft > a.lft and l.rgt < a.rgt), 2)
            else:
                a["balance"] = round(bmap.get(a.name, 0.0), 2)
            # depth from ancestor count
            a["depth"] = sum(1 for x in accounts if x.lft < a.lft and x.rgt > a.rgt)
        return {"accounts": accounts, "currency": currency, "company": company}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_chart_of_accounts")
        frappe.throw(str(e))


# ─────────────────────────── SETTINGS: COMPANY & USER ───────────────────────────

_COMPANY_EDITABLE = (
    "company_name", "tax_id", "phone_no", "email", "website", "company_logo",
    "country", "default_currency", "date_of_establishment", "company_description",
)


@frappe.whitelist()
def get_company_profile():
    """Return the editable company profile used by the portal Settings page."""
    company = _get_company()
    if not company:
        return {}
    d = frappe.get_doc("Company", company)
    return {
        "name": d.name,
        "company_name": d.company_name,
        "abbr": d.abbr,
        "default_currency": d.default_currency,
        "country": d.country,
        "tax_id": d.tax_id,
        "phone_no": d.get("phone_no"),
        "email": d.get("email"),
        "website": d.get("website"),
        "company_logo": d.get("company_logo"),
        "date_of_establishment": str(d.get("date_of_establishment") or ""),
        "company_description": d.get("company_description"),
    }


@frappe.whitelist()
def update_company_profile(**kwargs):
    """Update whitelisted Company fields. Requires write permission on Company."""
    company = _get_company()
    if not company:
        frappe.throw("No company found.")
    if not frappe.has_permission("Company", ptype="write", doc=company):
        frappe.throw("You do not have permission to edit company details.")
    doc = frappe.get_doc("Company", company)
    changed = False
    for key in _COMPANY_EDITABLE:
        if key in kwargs:
            val = kwargs.get(key)
            if val == "":
                val = None
            doc.set(key, val)
            changed = True
    if changed:
        doc.save()
        frappe.db.commit()
    return get_company_profile()


_USER_EDITABLE = ("first_name", "last_name", "full_name", "phone", "mobile_no",
                  "user_image", "time_zone", "language")


@frappe.whitelist()
def get_user_profile():
    """Return the signed-in user's editable profile."""
    user = frappe.session.user
    d = frappe.get_doc("User", user)
    return {
        "name": d.name,
        "email": d.email,
        "first_name": d.first_name,
        "last_name": d.last_name,
        "full_name": d.full_name,
        "phone": d.get("phone"),
        "mobile_no": d.get("mobile_no"),
        "user_image": d.get("user_image"),
        "time_zone": d.get("time_zone"),
        "language": d.get("language"),
        "roles": [r.role for r in (d.get("roles") or [])],
    }


@frappe.whitelist()
def update_user_profile(**kwargs):
    """Update the signed-in user's own profile fields."""
    user = frappe.session.user
    if user == "Guest":
        frappe.throw("You must be signed in to update your profile.")
    doc = frappe.get_doc("User", user)
    for key in _USER_EDITABLE:
        if key in kwargs:
            doc.set(key, kwargs.get(key) or None)
    # keep full_name in sync when names change but no explicit full_name given
    if ("first_name" in kwargs or "last_name" in kwargs) and "full_name" not in kwargs:
        doc.full_name = " ".join(filter(None, [doc.first_name, doc.last_name])) or doc.full_name
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return get_user_profile()
