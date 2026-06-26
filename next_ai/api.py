import frappe
import json
from frappe.utils import cint, flt, cstr, nowdate, getdate
from next_ai.ai.uae_tax_knowledge import UAE_TAX_KNOWLEDGE, UAE_TAX_KNOWLEDGE_VERSION
# Re-exported so the portal can call them at next_ai.api.* (they stay whitelisted).
from next_ai.ai.vat_advisory import advise_sales_vat, advise_purchase_vat  # noqa: F401
from next_ai.ai.corporate_tax_advisory import (  # noqa: F401
    get_corporate_tax_status, advise_purchase_ct,
)
from next_ai.crm_outreach import (  # noqa: F401
    get_outreach_audiences, preview_recipients, send_outreach_email,
)
from next_ai.crm_extra import (  # noqa: F401
    list_contacts, save_contact, list_followups, create_followup, complete_followup,
    log_activity, get_timeline, list_campaigns, create_campaign, import_leads,
    preview_whatsapp, send_whatsapp_outreach, get_sales_team,
)


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


def _ollama_config():
    settings = frappe.get_single("NextAI Settings")
    base = (settings.get("ollama_base_url") or "http://localhost:11434").rstrip("/")
    model = settings.get("ollama_model") or "llama3.1"
    return base, model


def _ollama_tools():
    """Convert the Anthropic-style _AI_TOOLS to Ollama's function-tool format."""
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("input_schema", {})}}
            for t in _AI_TOOLS]


def _ollama_chat(messages, system=None, tools=None):
    """Call a local/remote Ollama server's /api/chat. Returns the message dict
    {role, content, tool_calls?}. Raises on connection/HTTP errors."""
    import requests
    base, model = _ollama_config()
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    payload = {"model": model, "messages": msgs, "stream": False}
    if tools:
        payload["tools"] = tools
    try:
        r = requests.post(f"{base}/api/chat", json=payload, timeout=180)
        r.raise_for_status()
    except Exception as e:
        frappe.throw(f"Could not reach Ollama at {base}. Is the server running and the model pulled? ({e})")
    return (r.json() or {}).get("message", {}) or {}


def _ollama_tool_args(call):
    args = (call.get("function") or {}).get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    return args


def _ollama_textual_toolcalls(content):
    """Smaller Ollama models often emit tool calls as raw JSON text in `content`
    instead of using the structured `tool_calls` field. Parse those out so the
    agent still works. Returns a list of {function:{name,arguments}} or []."""
    import re
    s = (content or "").strip()
    if not s or "{" not in s:
        return []
    s = re.sub(r"^```(?:json)?|```$", "", s).strip()
    candidates = []
    try:
        candidates.append(json.loads(s))
    except Exception:
        for m in re.finditer(r"\{(?:[^{}]|\{[^{}]*\})*\}", s):
            try:
                candidates.append(json.loads(m.group()))
            except Exception:
                continue
    calls = []
    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or (obj.get("function") or {}).get("name")
        raw = obj.get("parameters")
        if raw is None:
            raw = obj.get("arguments")
        if raw is None and isinstance(obj.get("function"), dict):
            raw = obj["function"].get("arguments")
        if not name:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        calls.append({"function": {"name": name, "arguments": raw or {}}})
    return calls


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
        platform = (frappe.db.get_single_value("NextAI Settings", "platform") or "").strip()
        client = _anthropic_client()
        if platform == "Ollama":
            provider = "ollama"            # explicit choice wins
        elif client is not None:
            provider = "anthropic"
        else:
            provider = "gemini"

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

            if provider == "ollama":
                convo = [{"role": m["role"], "content": m["content"]} for m in messages]
                for _ in range(8):
                    msg = _ollama_chat(convo, system=system, tools=_ollama_tools())
                    tcs = msg.get("tool_calls") or []
                    if not tcs:
                        # Fallback: model emitted the tool call as raw JSON text.
                        tcs = _ollama_textual_toolcalls(msg.get("content"))
                    if not tcs:
                        text = msg.get("content") or "Done."
                        return finish({"type": "message", "text": text}, text)
                    write_calls = [c for c in tcs if (c.get("function") or {}).get("name") in _AI_WRITE_TOOLS]
                    if write_calls:
                        c = write_calls[0]
                        name = c["function"]["name"]
                        inp = _ollama_tool_args(c)
                        summary = _ai_summarize_action(name, inp)
                        return finish(
                            {"type": "confirm", "tool": name, "input": inp,
                             "summary": summary, "text": msg.get("content") or ""},
                            f"Pending user confirmation: {summary}",
                        )
                    convo.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
                    for c in tcs:
                        name = (c.get("function") or {}).get("name")
                        out = _ai_run_readonly_tool(name, _ollama_tool_args(c), company)
                        convo.append({"role": "tool", "content": json.dumps(out, default=str)})
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
        if provider == "ollama":
            convo = [{"role": m["role"], "content": m["content"]} for m in messages]
            msg = _ollama_chat(convo, system=system)
            text = msg.get("content") or "…"
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
def get_team_directory():
    """Team members with roles, contact details, last activity and open task load."""
    users = frappe.get_all("User",
        filters={"enabled": 1, "user_type": "System User", "name": ["not in", ["Administrator", "Guest"]]},
        fields=["name", "full_name", "user_image", "mobile_no", "phone", "last_active",
                "location"],
        order_by="full_name asc", limit=500)
    out = []
    for u in users:
        roles = frappe.get_all("Has Role", filters={"parent": u.name, "parenttype": "User"}, pluck="role")
        roles = [r for r in roles if r not in ("All", "Guest")]
        open_tasks = frappe.db.count("ToDo", {"allocated_to": u.name, "status": "Open"})
        out.append({
            "name": u.name, "full_name": u.full_name or u.name, "user_image": u.user_image,
            "mobile_no": u.mobile_no or u.phone, "designation": None,
            "location": u.location, "last_active": u.last_active,
            "roles": roles[:6], "role_count": len(roles), "open_tasks": open_tasks,
        })
    return out


@frappe.whitelist()
def broadcast_to_team(subject, message=None, recipients=None, role=None, priority="Normal"):
    """Send an in-system notification to selected active team members."""
    if frappe.session.user in ("Guest", None, ""):
        frappe.throw("You must be signed in.")
    subject = cstr(subject).strip()
    if not subject:
        frappe.throw("Subject is required.")
    priority = cstr(priority or "Normal").strip()
    if priority not in ("Normal", "Important", "Urgent"):
        priority = "Normal"

    selected = []
    if recipients:
        try:
            selected = json.loads(recipients) if isinstance(recipients, str) else recipients
        except Exception:
            selected = []
        selected = [cstr(u).strip() for u in selected if cstr(u).strip()]

    filters = {
        "enabled": 1,
        "user_type": "System User",
        "name": ["not in", ["Administrator", "Guest"]],
    }
    if selected:
        filters["name"] = ["in", selected]

    users = frappe.get_all("User", filters=filters, pluck="name")
    role = cstr(role or "").strip()
    if role:
        role_users = set(frappe.get_all(
            "Has Role",
            filters={"role": role, "parenttype": "User", "parent": ["in", users or [""]]},
            pluck="parent",
        ))
        users = [u for u in users if u in role_users]

    users = [u for u in users if u != frappe.session.user]
    if not users:
        frappe.throw("No active team members match this audience.")

    badge = {"Normal": "", "Important": "Important: ", "Urgent": "Urgent: "}[priority]
    subject = badge + subject
    sent = 0
    for u in users:
        try:
            frappe.get_doc({
                "doctype": "Notification Log", "subject": subject,
                "email_content": cstr(message or ""), "for_user": u, "type": "Alert",
                "from_user": frappe.session.user,
            }).insert(ignore_permissions=True)
            sent += 1
        except Exception:
            pass
    return {"ok": True, "sent": sent}


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


@frappe.whitelist()
def get_top_expense_trends(company=None, limit=5):
    """Top expense accounts for the current fiscal year with a comparison against
    the same accounts in the prior fiscal year (amount and % change)."""
    try:
        if not company:
            company = _get_company()
        if not company:
            return {"period": "", "rows": []}
        limit = cint(limit) or 5
        currency = frappe.get_value("Company", company, "default_currency") or "AED"

        fy = frappe.get_all(
            "Fiscal Year",
            filters=[["year_start_date", "<=", frappe.utils.today()],
                     ["year_end_date", ">=", frappe.utils.today()]],
            fields=["year_start_date", "year_end_date"], limit=1)
        if fy:
            fd, td = cstr(fy[0].year_start_date), cstr(fy[0].year_end_date)
        else:
            yr = frappe.utils.today()[:4]
            fd, td = f"{yr}-01-01", f"{yr}-12-31"
        prev_fd = frappe.utils.add_years(getdate(fd), -1)
        prev_td = frappe.utils.add_years(getdate(td), -1)

        def expense_totals(start, end):
            rows = frappe.db.sql("""
                SELECT g.account AS account, IFNULL(SUM(g.debit - g.credit), 0) AS amount
                FROM `tabGL Entry` g
                INNER JOIN `tabAccount` a ON a.name = g.account
                WHERE g.company=%(co)s AND g.is_cancelled=0 AND a.root_type='Expense'
                  AND g.posting_date BETWEEN %(fd)s AND %(td)s
                GROUP BY g.account
            """, {"co": company, "fd": start, "td": end}, as_dict=True)
            return {r.account: flt(r.amount) for r in rows}

        current = expense_totals(fd, td)
        previous = expense_totals(prev_fd, prev_td)

        top = sorted(current.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        rows = []
        for account, amount in top:
            if amount <= 0:
                continue
            prev = flt(previous.get(account, 0))
            if prev > 0:
                change_pct = round((amount - prev) / prev * 100, 1)
            else:
                change_pct = None  # no prior-year baseline
            rows.append({
                "account": account,
                "account_name": frappe.get_value("Account", account, "account_name") or account,
                "current": round(amount, 2),
                "previous": round(prev, 2),
                "change_pct": change_pct,
            })
        return {
            "currency": currency,
            "period": f"{fd} → {td}",
            "prev_period": f"{cstr(prev_fd)} → {cstr(prev_td)}",
            "rows": rows,
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_top_expense_trends")
        return {"period": "", "rows": []}


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
        payment_terms = frappe.get_all("Payment Terms Template", pluck="name", order_by="name")
        terms = frappe.get_all("Terms and Conditions", pluck="name", order_by="name")
        uoms = frappe.get_all("UOM", pluck="name", order_by="name")
        tax_categories = frappe.get_all("Tax Category", pluck="name", order_by="name")
        return {"accounts": accounts, "templates": [t.name for t in templates], "currency": currency,
                "payment_terms": payment_terms, "terms": terms, "uoms": uoms,
                "tax_categories": tax_categories}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_transaction_meta")
        return {"accounts": [], "templates": [], "currency": "AED"}


# ── Customer ─────────────────────────────────────────────────────
CUSTOMER_VAT_TREATMENTS = {
    "VAT Registered", "VAT Not Registered", "GCC VAT Registered",
    "GCC VAT Not Registered", "Non-GCC", "Designated Zone", "Exempt",
    "Out of Scope",
}


# UAE compliance fields installed on Customer by customer_tax_setup.py.
UAE_COMPLIANCE_FIELDS = {
    "trade_license_number": "custom_trade_license_number",
    "trade_license_expiry": "custom_trade_license_expiry",
    "emirates_id": "custom_emirates_id",
    "vat_number": "custom_vat_number",
    "vat_registration_date": "custom_vat_registration_date",
    "vat_period_stagger": "custom_vat_period_stagger",
    "corporate_tax_number": "custom_corporate_tax_number",
    "corporate_tax_period_date": "custom_corporate_tax_period_date",
}


def _apply_uae_compliance(doc, values):
    """Set the UAE compliance custom fields on a Customer doc from a values dict."""
    for arg, fieldname in UAE_COMPLIANCE_FIELDS.items():
        val = values.get(arg)
        if val is not None and doc.meta.has_field(fieldname):
            doc.set(fieldname, cstr(val).strip() or None)


def _validate_customer_tax_details(vat_treatment=None, tax_id=None,
                                   tax_category=None, country=None):
    vat_treatment = cstr(vat_treatment).strip()
    tax_id = cstr(tax_id).strip()
    country = cstr(country).strip()
    if vat_treatment and vat_treatment not in CUSTOMER_VAT_TREATMENTS:
        frappe.throw("Please select a valid VAT treatment.")
    if tax_category and not frappe.db.exists("Tax Category", tax_category):
        frappe.throw("Please select a valid Tax Category.")
    if country and not frappe.db.exists("Country", country):
        frappe.throw("Please select a valid country.")
    if vat_treatment in {"VAT Registered", "GCC VAT Registered"} and not tax_id:
        frappe.throw("A VAT/TRN number is required for VAT-registered customers.")
    if country == "United Arab Emirates" and tax_id:
        digits = "".join(char for char in tax_id if char.isdigit())
        if len(digits) != 15 or len(digits) != len(tax_id):
            frappe.throw("A UAE Tax Registration Number must contain exactly 15 digits.")


def _upsert_customer_address(customer, customer_name, address_line1=None,
                             address_line2=None, city=None, state=None,
                             pincode=None, country=None):
    values = [address_line1, address_line2, city, state, pincode]
    if not any(cstr(value).strip() for value in values):
        return None
    if not cstr(address_line1).strip() or not cstr(city).strip() or not cstr(country).strip():
        frappe.throw("Address line 1, city, and country are required when adding an address.")

    address_name = frappe.db.sql("""
        SELECT a.name
        FROM `tabAddress` a
        INNER JOIN `tabDynamic Link` dl ON dl.parent=a.name
        WHERE dl.parenttype='Address' AND dl.link_doctype='Customer'
          AND dl.link_name=%s
        ORDER BY a.is_primary_address DESC, a.modified DESC
        LIMIT 1
    """, customer)
    address = (frappe.get_doc("Address", address_name[0][0]) if address_name else
               frappe.new_doc("Address"))
    address.update({
        "address_title": customer_name,
        "address_type": "Billing",
        "address_line1": cstr(address_line1).strip(),
        "address_line2": cstr(address_line2).strip(),
        "city": cstr(city).strip(),
        "state": cstr(state).strip(),
        "pincode": cstr(pincode).strip(),
        "country": cstr(country).strip(),
        "is_primary_address": 1,
    })
    if address.is_new():
        address.append("links", {"link_doctype": "Customer", "link_name": customer})
        address.insert(ignore_permissions=True)
    else:
        address.save(ignore_permissions=True)
    return address.name


@frappe.whitelist()
def create_customer(customer_name, customer_type="Company", customer_group=None,
                    territory=None, mobile_no=None, email_id=None, tax_id=None,
                    vat_treatment=None, tax_category=None, address_line1=None,
                    address_line2=None, city=None, state=None, pincode=None,
                    country=None, trade_license_number=None, trade_license_expiry=None,
                    emirates_id=None, vat_number=None, vat_registration_date=None,
                    vat_period_stagger=None, corporate_tax_number=None,
                    corporate_tax_period_date=None):
    try:
        _validate_customer_tax_details(vat_treatment, tax_id, tax_category, country)
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
        if tax_category:
            doc.tax_category = tax_category
        if vat_treatment and doc.meta.has_field("custom_vat_treatment"):
            doc.custom_vat_treatment = vat_treatment
        _apply_uae_compliance(doc, {
            "trade_license_number": trade_license_number,
            "trade_license_expiry": trade_license_expiry, "emirates_id": emirates_id,
            "vat_number": vat_number, "vat_registration_date": vat_registration_date,
            "vat_period_stagger": vat_period_stagger,
            "corporate_tax_number": corporate_tax_number,
            "corporate_tax_period_date": corporate_tax_period_date,
        })

        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        address = _upsert_customer_address(
            doc.name, doc.customer_name, address_line1, address_line2, city,
            state, pincode, country,
        )
        return {"name": doc.name, "customer_name": doc.customer_name,
                "address": address}

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

    primary_address = addresses[0] if addresses else {}
    return {
        "customer": {
            "name": doc.name,
            "customer_name": doc.customer_name,
            "customer_type": doc.customer_type,
            "tax_id": doc.tax_id,
            "tax_category": doc.tax_category,
            "vat_treatment": (doc.get("custom_vat_treatment")
                              if doc.meta.has_field("custom_vat_treatment") else None),
            **{arg: (cstr(doc.get(fieldname)) if doc.meta.has_field(fieldname) and doc.get(fieldname) else None)
               for arg, fieldname in UAE_COMPLIANCE_FIELDS.items()},
            "mobile_no": doc.mobile_no,
            "email_id": doc.email_id,
            "territory": doc.territory,
            "customer_group": doc.customer_group,
            "address_line1": primary_address.get("address_line1"),
            "address_line2": primary_address.get("address_line2"),
            "city": primary_address.get("city"),
            "state": primary_address.get("state"),
            "pincode": primary_address.get("pincode"),
            "country": primary_address.get("country"),
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
                         company=None, reference_no=None, reference_date=None, remarks=None):
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
            doc.reference_date = reference_date or posting_date or nowdate()
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


def _default_bank_cash_account(company, mode_of_payment=None):
    """Resolve a bank/cash GL account: Mode of Payment default first, then any
    Bank account, then any Cash account."""
    if mode_of_payment:
        acc = frappe.db.get_value("Mode of Payment Account",
                                  {"parent": mode_of_payment, "company": company}, "default_account")
        if acc:
            return acc
    for atype in ("Bank", "Cash"):
        acc = frappe.get_all("Account", filters={"company": company, "account_type": atype,
                                                  "is_group": 0}, pluck="name", limit=1)
        if acc:
            return acc[0]
    return None


@frappe.whitelist()
def record_party_payment(party_type, party, paid_amount, posting_date=None,
                         mode_of_payment="Cash", reference_no=None, reference_date=None,
                         company=None, bank_account=None):
    """Simplified on-account payment: Customer → Receive, Supplier → Pay.
    Bank/cash and party accounts are resolved automatically."""
    try:
        if frappe.session.user in ("Guest", None, ""):
            frappe.throw("You must be signed in.")
        if not company:
            company = _get_company()
        paid_amount = flt(paid_amount)
        if paid_amount <= 0:
            frappe.throw("Amount must be greater than zero.")

        from erpnext.accounts.party import get_party_account
        party_account = get_party_account(party_type, party, company)
        bank = cstr(bank_account) if bank_account else _default_bank_cash_account(company, mode_of_payment)
        if bank and not frappe.db.exists("Account", {"name": bank, "company": company, "is_group": 0}):
            frappe.throw("Please select a valid bank or cash account.")
        if not party_account or not bank:
            frappe.throw("Could not resolve the bank/cash or party account. Set up a Bank or Cash account first.")

        payment_type = "Receive" if party_type == "Customer" else "Pay"
        paid_from = party_account if payment_type == "Receive" else bank
        paid_to = bank if payment_type == "Receive" else party_account

        return create_payment_entry(payment_type, party_type, party, posting_date,
                                    paid_amount, paid_from, paid_to,
                                    mode_of_payment=mode_of_payment, company=company,
                                    reference_no=reference_no, reference_date=reference_date)
    except frappe.ValidationError:
        raise
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: record_party_payment")
        frappe.throw(str(e))


@frappe.whitelist()
def get_supplier_outstanding_bills(supplier, company=None):
    try:
        if not company:
            company = _get_company()
        return frappe.db.sql("""
            SELECT name, supplier, supplier_name, bill_no, posting_date, due_date,
                   currency, grand_total, outstanding_amount, status
            FROM `tabPurchase Invoice`
            WHERE docstatus = 1
              AND outstanding_amount > 0
              AND supplier = %(supplier)s
              AND company = %(company)s
            ORDER BY
              CASE WHEN due_date IS NOT NULL AND due_date < %(today)s THEN 0 ELSE 1 END,
              due_date ASC,
              posting_date ASC
        """, {"supplier": supplier, "company": company, "today": nowdate()}, as_dict=True)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_supplier_outstanding_bills")
        frappe.throw(str(e))


@frappe.whitelist()
def create_purchase_invoice_payment(purchase_invoice, amount, posting_date=None, reference_no=None,
                                    reference_date=None, mode_of_payment="Cash", bank_account=None):
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    source = frappe.get_doc("Purchase Invoice", purchase_invoice)
    if source.docstatus != 1 or flt(source.outstanding_amount) <= 0:
        frappe.throw("Purchase Invoice must be submitted with an outstanding amount.")
    amount = flt(amount)
    if amount <= 0 or amount > flt(source.outstanding_amount):
        frappe.throw("Payment must be greater than zero and cannot exceed the outstanding amount.")
    target = get_payment_entry(
        "Purchase Invoice", source.name, party_amount=amount,
        reference_date=posting_date or nowdate(), ignore_permissions=True,
    )
    target.posting_date = posting_date or nowdate()
    target.paid_amount = amount
    target.received_amount = amount
    target.mode_of_payment = mode_of_payment
    if bank_account:
        if not frappe.db.exists("Account", {"name": bank_account, "company": source.company, "is_group": 0}):
            frappe.throw("Please select a valid bank or cash account.")
        target.paid_from = bank_account
    target.reference_no = reference_no or f"PAY-{source.name}-{posting_date or nowdate()}"
    target.reference_date = reference_date or posting_date or nowdate()
    target.flags.ignore_permissions = True
    target.insert(ignore_permissions=True)
    return {"name": target.name, "doctype": target.doctype, "status": "Draft", "grand_total": amount}


@frappe.whitelist()
def get_party_payment_summary(party_type, party, company=None):
    """Payment history + 6-month trend + outstanding for a customer/supplier."""
    try:
        if not company:
            company = _get_company()
        rows = frappe.get_all("Payment Entry",
            filters={"party_type": party_type, "party": party, "docstatus": 1},
            fields=["name", "posting_date", "paid_amount", "mode_of_payment",
                    "reference_no", "payment_type"],
            order_by="posting_date desc", limit=50)
        total = sum(flt(r.paid_amount) for r in rows)

        # 6-month trend
        import collections
        trend = collections.OrderedDict()
        today = getdate(frappe.utils.today())
        for i in range(5, -1, -1):
            d = frappe.utils.add_months(today, -i)
            trend[d.strftime("%Y-%m")] = 0.0
        for r in rows:
            key = getdate(r.posting_date).strftime("%Y-%m")
            if key in trend:
                trend[key] += flt(r.paid_amount)

        inv_dt = "Sales Invoice" if party_type == "Customer" else "Purchase Invoice"
        party_field = "customer" if party_type == "Customer" else "supplier"
        outstanding = flt(frappe.db.sql(f"""
            SELECT IFNULL(SUM(outstanding_amount),0) FROM `tab{inv_dt}`
            WHERE docstatus=1 AND {party_field}=%s AND outstanding_amount>0
        """, party)[0][0])

        return {
            "currency": frappe.get_value("Company", company, "default_currency") or "AED",
            "payments": rows,
            "total_paid": total,
            "outstanding": outstanding,
            "trend": [{"month": k, "amount": v} for k, v in trend.items()],
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_party_payment_summary")
        return {"payments": [], "total_paid": 0, "outstanding": 0, "trend": [], "currency": "AED"}


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
        # Auto-create project + tasks for items configured for it (idempotent;
        # the Sales Order on_submit hook may have already done this).
        make_projects_from_sales_order(doc)
        projects = frappe.get_all("Project", filters={"sales_order": doc.name}, pluck="name")
        return {"name": doc.name, "grand_total": doc.grand_total, "status": doc.status,
                "projects_created": projects}
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
                    email_id=None, tax_id=None, territory=None,
                    customer_display_name=None, vat_treatment=None,
                    tax_category=None, address_line1=None, address_line2=None,
                    city=None, state=None, pincode=None, country=None,
                    customer_group=None, trade_license_number=None,
                    trade_license_expiry=None, emirates_id=None, vat_number=None,
                    vat_registration_date=None, vat_period_stagger=None,
                    corporate_tax_number=None, corporate_tax_period_date=None):
    try:
        _validate_customer_tax_details(vat_treatment, tax_id, tax_category, country)
        doc = frappe.get_doc("Customer", customer_name)
        if customer_display_name:
            doc.customer_name = customer_display_name
        if customer_type:
            doc.customer_type = customer_type
        if mobile_no is not None:
            doc.mobile_no = mobile_no
        if email_id is not None:
            doc.email_id = email_id
        if tax_id is not None:
            doc.tax_id = tax_id
        if territory is not None:
            doc.territory = territory
        if customer_group:
            doc.customer_group = customer_group
        if tax_category is not None:
            doc.tax_category = tax_category
        if vat_treatment is not None and doc.meta.has_field("custom_vat_treatment"):
            doc.custom_vat_treatment = vat_treatment
        _apply_uae_compliance(doc, {
            "trade_license_number": trade_license_number,
            "trade_license_expiry": trade_license_expiry, "emirates_id": emirates_id,
            "vat_number": vat_number, "vat_registration_date": vat_registration_date,
            "vat_period_stagger": vat_period_stagger,
            "corporate_tax_number": corporate_tax_number,
            "corporate_tax_period_date": corporate_tax_period_date,
        })
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        address = _upsert_customer_address(
            doc.name, doc.customer_name, address_line1, address_line2, city,
            state, pincode, country,
        )
        return {"name": doc.name, "customer_name": doc.customer_name,
                "address": address}
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


# Default depreciation policy (Straight Line):
#   • Motor vehicles → 10% per year for 10 years
#   • All other categories → 20% per year for 5 years
def _depreciation_policy(category_name):
    n = (category_name or "").lower()
    if any(k in n for k in ("motor", "vehicle", "car", "truck", "van")):
        return {"years": 10, "rate": 10}
    return {"years": 5, "rate": 20}


def _resolve_depreciation_accounts(company):
    """Best-effort lookup of the three accounts an Asset Category needs to depreciate."""
    comp = frappe.get_doc("Company", company)

    def first_account(account_type=None, like=None, root_type=None):
        filters = {"company": company, "is_group": 0}
        if account_type:
            filters["account_type"] = account_type
        if root_type:
            filters["root_type"] = root_type
        rows = frappe.get_all("Account", filters=filters,
                              or_filters=({"account_name": ["like", like]} if like else None),
                              pluck="name", limit=1)
        return rows[0] if rows else None

    fixed_asset = first_account(account_type="Fixed Asset")
    accumulated = comp.get("accumulated_depreciation_account") or \
        first_account(account_type="Accumulated Depreciation") or \
        first_account(like="%Depreciation%", root_type="Asset")
    expense = comp.get("depreciation_expense_account") or \
        first_account(like="%Depreciation%", root_type="Expense")

    if fixed_asset and accumulated and expense:
        return {"fixed_asset_account": fixed_asset,
                "accumulated_depreciation_account": accumulated,
                "depreciation_expense_account": expense}
    return None


def _ensure_asset_category(category_name, company):
    """Create the Asset Category if missing, configured with the standard depreciation
    schedule + the company's depreciation accounts. Returns True if depreciation is set up."""
    pol = _depreciation_policy(category_name)
    accounts = _resolve_depreciation_accounts(company)

    if frappe.db.exists("Asset Category", category_name):
        cat = frappe.get_doc("Asset Category", category_name)
        # Backfill depreciation config if a previously-created category lacks it.
        changed = False
        if accounts and not cat.finance_books:
            cat.append("finance_books", {
                "depreciation_method": "Straight Line",
                "total_number_of_depreciations": pol["years"],
                "frequency_of_depreciation": 12,
                "rate_of_depreciation": pol["rate"],
            })
            changed = True
        if accounts and not any(a.company_name == company for a in cat.accounts):
            cat.append("accounts", {"company_name": company, **accounts})
            changed = True
        if changed:
            cat.flags.ignore_permissions = True
            cat.save(ignore_permissions=True)
        return bool(cat.finance_books and cat.accounts)

    cat = frappe.get_doc({
        "doctype": "Asset Category",
        "asset_category_name": category_name,
    })
    if accounts:
        cat.append("finance_books", {
            "depreciation_method": "Straight Line",
            "total_number_of_depreciations": pol["years"],
            "frequency_of_depreciation": 12,
            "rate_of_depreciation": pol["rate"],
        })
        cat.append("accounts", {"company_name": company, **accounts})
    cat.flags.ignore_permissions = True
    cat.insert(ignore_permissions=True)
    return bool(accounts)


def _ensure_asset_item(category_name, company):
    """Ensure a fixed-asset Item exists for this category (ERPNext requires every Asset
    to link to an Item). Sets an asset naming series so each Asset gets a proper number."""
    item_code = f"Asset - {category_name}"
    if frappe.db.exists("Item", item_code):
        return item_code
    item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") \
        or frappe.db.get_value("Item Group", {"name": "All Item Groups"}, "name")
    item = frappe.get_doc({
        "doctype": "Item",
        "item_code": item_code,
        "item_name": category_name,
        "item_group": item_group,
        "is_fixed_asset": 1,
        "is_stock_item": 0,
        "asset_category": category_name,
        "asset_naming_series": "ACC-ASS-.YYYY.-",
    })
    item.flags.ignore_permissions = True
    item.insert(ignore_permissions=True)
    return item_code


@frappe.whitelist()
def create_asset(asset_name, asset_category, purchase_date, gross_purchase_amount, company=None,
                 available_for_use_date=None, location=None, custodian=None):
    try:
        if not company:
            company = _get_company()
        avail = available_for_use_date or purchase_date
        # Auto-create the category (with depreciation schedule) if it doesn't exist yet.
        can_depreciate = _ensure_asset_category(asset_category, company) if asset_category else False
        # Every Asset must link to a fixed-asset Item — provision one per category.
        item_code = _ensure_asset_item(asset_category, company) if asset_category else None

        doc = frappe.get_doc({
            "doctype": "Asset",
            "asset_name": asset_name,
            "item_code": item_code,
            "asset_category": asset_category,
            "company": company,
            "purchase_date": purchase_date,
            "available_for_use_date": avail,
            "gross_purchase_amount": flt(gross_purchase_amount),
            "location": location,
            "custodian": custodian,
        })
        if can_depreciate:
            # Enable automatic depreciation — ERPNext copies the category's finance
            # books and its daily scheduler posts the depreciation entries.
            doc.calculate_depreciation = 1
            doc.append("finance_books", {
                "depreciation_method": "Straight Line",
                "total_number_of_depreciations": _depreciation_policy(asset_category)["years"],
                "frequency_of_depreciation": 12,
                "rate_of_depreciation": _depreciation_policy(asset_category)["rate"],
                "depreciation_start_date": avail,
            })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)

        # Submit so the depreciation schedule is generated; keep the draft on failure.
        submitted = False
        if can_depreciate:
            try:
                doc.submit()
                submitted = True
            except Exception:
                frappe.log_error(frappe.get_traceback(), "Portal: Asset submit (depreciation)")
        return {"name": doc.name, "asset_name": doc.asset_name,
                "depreciation": can_depreciate, "submitted": submitted}
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
def get_customer_form_metadata():
    """Return validated options used by the customer create/edit form."""
    return {
        "countries": frappe.get_all("Country", pluck="name", order_by="name", limit=300),
        "tax_categories": frappe.get_all("Tax Category", pluck="name", order_by="name", limit=300),
        "customer_groups": frappe.get_all("Customer Group", pluck="name", order_by="name", limit=300),
        "territories": frappe.get_all("Territory", pluck="name", order_by="name", limit=300),
        "vat_treatments": sorted(CUSTOMER_VAT_TREATMENTS),
    }


@frappe.whitelist()
def get_portal_translations(language, messages=None):
    """Return installed Frappe/ERPNext translations for portal UI messages."""
    language = cstr(language).strip().lower()
    if language not in {"en", "ar", "tr", "ru", "es"}:
        frappe.throw("Unsupported portal language.")
    if language == "en":
        return {}
    requested = frappe.parse_json(messages) if isinstance(messages, str) else (messages or [])
    if not isinstance(requested, list) or len(requested) > 400:
        frappe.throw("A maximum of 400 translation messages can be requested.")
    from frappe.translate import get_all_translations

    catalog = get_all_translations(language)
    result = {}
    for message in requested:
        source = cstr(message).strip()
        if not source or len(source) > 300:
            continue
        translated = catalog.get(source)
        if translated and translated != source:
            result[source] = translated
    return result


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
                    designation=None, company_email=None, cell_number=None,
                    employment_type=None, personal_email=None, branch=None,
                    salary_currency=None, salary_mode=None, payroll_cost_center=None,
                    bank_name=None, bank_ac_no=None, iban=None, passport_number=None):
    try:
        if not company:
            company = _get_company()
        if not company:
            frappe.throw("No company found.")

        # Split full name into first / middle / last (first_name is mandatory)
        name_parts = (employee_name or "").strip().split()
        first_name = name_parts[0] if name_parts else employee_name
        last_name = name_parts[-1] if len(name_parts) > 1 else None
        middle_name = " ".join(name_parts[1:-1]) if len(name_parts) > 2 else None

        doc = frappe.get_doc({
            "doctype": "Employee",
            "employee_name": employee_name,
            "first_name": first_name,
            "gender": gender,
            "date_of_joining": date_of_joining or nowdate(),
            "status": status,
            "company": company,
        })
        if last_name:
            doc.last_name = last_name
        if middle_name:
            doc.middle_name = middle_name
        # Map optional fields onto the doc only when provided
        optional = {
            "date_of_birth": date_of_birth,
            "department": department,
            "designation": designation,
            "company_email": company_email,
            "cell_number": cell_number,
            "employment_type": employment_type,
            "personal_email": personal_email,
            "branch": branch,
            "salary_currency": salary_currency,
            "salary_mode": salary_mode,
            "payroll_cost_center": payroll_cost_center,
            "bank_name": bank_name,
            "bank_ac_no": bank_ac_no,
            "iban": iban,
            "passport_number": passport_number,
        }
        for field, value in optional.items():
            if value:
                doc.set(field, value)

        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "employee_name": doc.employee_name}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Employee")
        frappe.throw(str(e))


@frappe.whitelist()
def update_employee_payroll(employee, salary_currency=None, salary_mode=None,
                            payroll_cost_center=None, bank_name=None, bank_ac_no=None,
                            iban=None, passport_number=None, cell_number=None,
                            date_of_birth=None, company_email=None, personal_email=None,
                            designation=None, department=None, employment_type=None):
    """Update payroll / bank / contact fields on an existing Employee — the data
    needed to generate salary slips and salary certificates."""
    try:
        doc = frappe.get_doc("Employee", employee)
        fields = {
            "salary_currency": salary_currency,
            "salary_mode": salary_mode,
            "payroll_cost_center": payroll_cost_center,
            "bank_name": bank_name,
            "bank_ac_no": bank_ac_no,
            "iban": iban,
            "passport_number": passport_number,
            "cell_number": cell_number,
            "date_of_birth": date_of_birth,
            "company_email": company_email,
            "personal_email": personal_email,
            "designation": designation,
            "department": department,
            "employment_type": employment_type,
        }
        for field, value in fields.items():
            if value is not None and value != "":
                doc.set(field, value)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        return {"name": doc.name, "employee_name": doc.employee_name}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: update_employee_payroll")
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
            order_by="attendance_date desc", limit_page_length=5000)
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
def apply_for_job(job_opening, applicant_name, email_id, phone_number=None, country=None,
                  cover_letter=None, resume_link=None, resume_base64=None, resume_filename=None):
    """Create a Job Applicant from the authenticated firm portal."""
    import base64
    import os
    from frappe.utils import nowdate, validate_email_address
    from frappe.utils.file_manager import save_file

    if frappe.session.user == "Guest":
        frappe.throw("Please sign in to apply for a job.", frappe.PermissionError)

    applicant_name = cstr(applicant_name).strip()
    email_id = cstr(email_id).strip().lower()
    if not applicant_name or not email_id:
        frappe.throw("Applicant name and email address are required.")
    validate_email_address(email_id, throw=True)

    opening = frappe.db.get_value(
        "Job Opening", job_opening,
        ["name", "status", "closes_on", "designation"], as_dict=True,
    )
    if not opening or opening.status != "Open":
        frappe.throw("This job opening is no longer accepting applications.")
    if opening.closes_on and getdate(opening.closes_on) < getdate(nowdate()):
        frappe.throw("The application deadline for this job opening has passed.")

    duplicate = frappe.db.exists("Job Applicant", {
        "email_id": email_id,
        "job_title": job_opening,
        "status": ["not in", ["Rejected"]],
    })
    if duplicate:
        frappe.throw(f"An active application already exists for {email_id} and this job opening.")

    if country and not frappe.db.exists("Country", country):
        frappe.throw("Please select a valid country.")

    doc = frappe.get_doc({
        "doctype": "Job Applicant",
        "applicant_name": applicant_name,
        "email_id": email_id,
        "phone_number": cstr(phone_number).strip(),
        "country": country or None,
        "job_title": job_opening,
        "designation": opening.designation,
        "status": "Open",
        "cover_letter": cover_letter,
        "resume_link": cstr(resume_link).strip(),
    })
    doc.insert(ignore_permissions=True)

    if resume_base64 and resume_filename:
        filename = os.path.basename(cstr(resume_filename))
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension not in {"pdf", "doc", "docx"}:
            frappe.throw("Resume must be a PDF, DOC, or DOCX file.")
        encoded = cstr(resume_base64).split(",", 1)[-1]
        try:
            content = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError):
            frappe.throw("The resume file could not be read.")
        if len(content) > 5 * 1024 * 1024:
            frappe.throw("Resume file must be 5 MB or smaller.")
        file_doc = save_file(filename, content, "Job Applicant", doc.name, is_private=1)
        doc.db_set("resume_attachment", file_doc.file_url)

    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def get_employee_details(employee):
    """Full profile: info + last 6 salary slips + leave balances + leave taken."""
    try:
        import datetime
        cur_year = datetime.date.today().year

        emp = frappe.get_value("Employee", employee,
            ["name","employee_name","department","designation","company",
             "date_of_joining","employment_type","gender","cell_number",
             "personal_email","company_email","branch","grade","status",
             "date_of_birth","passport_number","salary_currency","salary_mode",
             "bank_name","bank_ac_no","iban"],
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
def get_salary_certificate(employee, salary_slip=None):
    """Gather all data needed to generate a UAE-style salary certificate.

    Returns employee info, company/letterhead info, and a salary breakdown
    (basic + allowances/earnings, deductions, gross & net) taken from the
    latest submitted Salary Slip (or the one passed in)."""
    try:
        from frappe.utils import money_in_words, get_datetime, formatdate

        emp = frappe.get_value("Employee", employee,
            ["name", "employee_name", "department", "designation", "company",
             "date_of_joining", "employment_type", "gender", "cell_number",
             "personal_email", "company_email", "branch", "grade", "status",
             "date_of_birth", "passport_number", "valid_upto", "bank_name",
             "bank_ac_no", "iban", "salary_currency", "salary_mode"],
            as_dict=True)
        if not emp:
            frappe.throw(f"Employee {employee} not found")

        # Optional UAE custom fields (nationality / Emirates ID / visa) if present
        meta_fields = {f.fieldname for f in frappe.get_meta("Employee").fields}
        for fld in ("custom_nationality", "nationality", "custom_emirates_id",
                    "emirates_id", "custom_visa_number", "visa_number",
                    "custom_labour_card_no", "labour_card_no"):
            if fld in meta_fields:
                emp[fld] = frappe.db.get_value("Employee", employee, fld)

        # Pick the salary slip: requested one, else latest submitted, else latest draft
        slip_name = salary_slip
        if not slip_name:
            slip_name = frappe.db.get_value("Salary Slip",
                {"employee": employee, "docstatus": 1},
                "name", order_by="start_date desc")
        if not slip_name:
            slip_name = frappe.db.get_value("Salary Slip",
                {"employee": employee, "docstatus": ["!=", 2]},
                "name", order_by="start_date desc")

        slip = None
        earnings, deductions = [], []
        if slip_name:
            sdoc = frappe.get_doc("Salary Slip", slip_name)
            slip = {
                "name": sdoc.name,
                "start_date": str(sdoc.start_date or ""),
                "end_date": str(sdoc.end_date or ""),
                "gross_pay": flt(sdoc.gross_pay),
                "total_deduction": flt(sdoc.total_deduction),
                "net_pay": flt(sdoc.net_pay),
                "currency": sdoc.get("currency") or emp.get("salary_currency"),
            }
            for e in sdoc.get("earnings", []):
                if flt(e.amount) != 0:
                    earnings.append({"component": e.salary_component, "amount": flt(e.amount)})
            for de in sdoc.get("deductions", []):
                if flt(de.amount) != 0:
                    deductions.append({"component": de.salary_component, "amount": flt(de.amount)})

        # Company / letter-head info
        company_name = emp.get("company") or _get_company()
        comp = frappe.get_value("Company", company_name,
            ["name", "country", "tax_id", "phone_no", "email",
             "company_logo", "default_currency", "registration_details"],
            as_dict=True) or {}

        # Company primary address (linked via Dynamic Link)
        address = {}
        try:
            addr_name = frappe.db.sql("""
                SELECT parent FROM `tabDynamic Link`
                WHERE link_doctype='Company' AND link_name=%s
                  AND parenttype='Address'
                ORDER BY parent LIMIT 1
            """, company_name)
            if addr_name:
                address = frappe.get_value("Address", addr_name[0][0],
                    ["address_line1", "address_line2", "city", "state",
                     "country", "pincode", "phone", "email_id"],
                    as_dict=True) or {}
        except Exception:
            pass

        currency = (slip or {}).get("currency") or emp.get("salary_currency") \
            or comp.get("default_currency") or "AED"
        net_pay = (slip or {}).get("net_pay") or 0
        amount_in_words = money_in_words(net_pay, currency) if net_pay else ""

        return {
            "employee": emp,
            "company": comp,
            "address": address,
            "slip": slip,
            "earnings": earnings,
            "deductions": deductions,
            "currency": currency,
            "amount_in_words": amount_in_words,
            "issue_date": formatdate(nowdate(), "dd MMMM yyyy"),
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_salary_certificate")
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
    """Create a GPS-verified Employee Checkin record (HRMS)."""
    try:
        from frappe.utils import now_datetime, get_datetime

        log_type = cstr(log_type).strip().upper()
        if log_type not in {"IN", "OUT"}:
            frappe.throw("Log type must be IN or OUT.")
        if latitude is None or longitude is None or not cstr(latitude).strip() or not cstr(longitude).strip():
            frappe.throw("GPS location is required for check-in and check-out.")
        latitude = flt(latitude)
        longitude = flt(longitude)
        if latitude == 0 and longitude == 0:
            frappe.throw("A valid GPS location is required for check-in and check-out.")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            frappe.throw("The supplied GPS coordinates are invalid.")

        emp_doc = frappe.get_value("Employee", employee, ["name", "employee_name", "company"], as_dict=True)
        if not emp_doc:
            frappe.throw(f"Employee '{employee}' not found.")

        checkin_time = get_datetime(time) if time else now_datetime()

        doc = frappe.new_doc("Employee Checkin")
        doc.employee = emp_doc.name
        doc.employee_name = emp_doc.employee_name
        doc.log_type = log_type
        doc.time = checkin_time
        doc.latitude = latitude
        doc.longitude = longitude
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
                     remarks=None, company=None, title=None, currency=None,
                     conversion_rate=None, taxes_and_charges=None,
                     tax_category=None, additional_discount_percentage=None,
                     payment_terms_template=None, tc_name=None, terms=None,
                     order_type="Sales", save_as_draft=0):
    import json
    try:
        if not company:
            company = _get_company()
        items = json.loads(items) if isinstance(items, str) else items
        if not items:
            frappe.throw("Add at least one quotation item.")
        discount = flt(additional_discount_percentage)
        if discount < 0 or discount > 100:
            frappe.throw("Additional discount must be between 0 and 100 percent.")
        if taxes_and_charges and not frappe.db.exists(
            "Sales Taxes and Charges Template", {"name": taxes_and_charges, "company": company}
        ):
            frappe.throw("Please select a valid sales tax template for this company.")
        if payment_terms_template and not frappe.db.exists("Payment Terms Template", payment_terms_template):
            frappe.throw("Please select a valid Payment Terms Template.")
        if tc_name and not frappe.db.exists("Terms and Conditions", tc_name):
            frappe.throw("Please select valid Terms and Conditions.")
        if tax_category and not frappe.db.exists("Tax Category", tax_category):
            frappe.throw("Please select a valid Tax Category.")
        if currency and not frappe.db.exists("Currency", currency):
            frappe.throw("Please select a valid currency.")
        doc = frappe.get_doc({
            "doctype": "Quotation",
            "quotation_to": party_type,
            "party_name": party_name,
            "transaction_date": transaction_date,
            "valid_till": valid_till or frappe.utils.add_days(transaction_date, 30),
            "company": company,
            "title": title or None,
            "order_type": order_type or "Sales",
            "currency": currency or frappe.get_value("Company", company, "default_currency"),
            "conversion_rate": flt(conversion_rate) or 1,
            "taxes_and_charges": taxes_and_charges or None,
            "tax_category": tax_category or None,
            "apply_discount_on": "Grand Total",
            "additional_discount_percentage": discount,
            "payment_terms_template": payment_terms_template or None,
            "tc_name": tc_name or None,
            "terms": terms or (frappe.db.get_value("Terms and Conditions", tc_name, "terms") if tc_name else None),
            "remarks": remarks or "",
            "items": [
                {"item_code": i["item_code"], "qty": flt(i.get("qty", 1)),
                 "rate": flt(i.get("rate", 0)), "description": i.get("description", ""),
                 "uom": i.get("uom") or None,
                 "discount_percentage": flt(i.get("discount_percentage"))}
                for i in items
            ],
        })
        if taxes_and_charges:
            doc.append_taxes_from_master()
        doc.insert(ignore_permissions=True)
        if not cint(save_as_draft):
            doc.submit()
        return {"name": doc.name, "grand_total": flt(doc.grand_total),
                "status": doc.status, "docstatus": doc.docstatus, "ok": True}
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


def _clean_boarding_activities(activities):
    if isinstance(activities, str):
        activities = json.loads(activities or "[]")
    out = []
    for row in activities or []:
        activity_name = cstr(row.get("activity_name")).strip()
        if not activity_name:
            continue
        role = cstr(row.get("role")).strip()
        user = cstr(row.get("user")).strip()
        out.append({
            "activity_name": activity_name,
            "role": role if role and frappe.db.exists("Role", role) else None,
            "user": user if user and frappe.db.exists("User", user) else None,
            "required_for_employee_creation": cint(row.get("required_for_employee_creation", 1)),
            "description": row.get("description") or "",
            "task_weight": flt(row.get("task_weight") or 0),
            "begin_on": cint(row.get("begin_on") or 0),
            "duration": cint(row.get("duration") or 1),
        })
    return out


@frappe.whitelist()
def get_employee_assets(employee):
    try:
        return frappe.db.sql("""
            SELECT a.name, a.asset_name, a.asset_category, a.status, a.location, a.custodian,
                   a.purchase_date, a.gross_purchase_amount,
                   a.value_after_depreciation AS net_asset_value
            FROM `tabAsset` a
            WHERE a.docstatus < 2 AND a.custodian = %(employee)s
            ORDER BY a.asset_name
        """, {"employee": employee}, as_dict=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: get_employee_assets")
        return []


@frappe.whitelist()
def get_onboarding_data():
    try:
        templates = frappe.db.sql("""
            SELECT t.name, t.title, t.company, t.department, t.designation,
                   COUNT(a.name) AS activity_count
            FROM `tabEmployee Onboarding Template` t
            LEFT JOIN `tabEmployee Boarding Activity` a
                ON a.parent=t.name AND a.parenttype='Employee Onboarding Template'
            WHERE t.docstatus < 2
            GROUP BY t.name
            ORDER BY t.modified DESC
            LIMIT 100
        """, as_dict=True)
    except Exception:
        templates = []
    try:
        onboardings = frappe.get_all(
            "Employee Onboarding",
            fields=["name", "employee", "employee_name", "employee_onboarding_template",
                    "date_of_joining", "boarding_begins_on", "boarding_status", "docstatus"],
            filters={"docstatus": ["<", 2]},
            order_by="modified desc",
            limit=100,
        )
    except Exception:
        onboardings = []
    employees = get_employees(status="Active")
    assigned_assets = frappe.db.sql("""
        SELECT a.name, a.asset_name, a.asset_category, a.location, a.custodian,
               e.employee_name, a.value_after_depreciation AS net_asset_value
        FROM `tabAsset` a
        LEFT JOIN `tabEmployee` e ON e.name = a.custodian
        WHERE a.docstatus < 2 AND IFNULL(a.custodian, '') != ''
        ORDER BY a.modified DESC
        LIMIT 100
    """, as_dict=True)
    return {"templates": templates, "onboardings": onboardings,
            "employees": employees, "assigned_assets": assigned_assets}


@frappe.whitelist()
def create_onboarding_template(title, department=None, designation=None, company=None, activities=None):
    try:
        company = company or _get_company()
        doc = frappe.get_doc({
            "doctype": "Employee Onboarding Template",
            "title": title,
            "company": company,
        })
        if department and frappe.db.exists("Department", department):
            doc.department = department
        if designation and frappe.db.exists("Designation", designation):
            doc.designation = designation
        for row in _clean_boarding_activities(activities):
            doc.append("activities", row)
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "title": doc.title, "activity_count": len(doc.activities)}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: create_onboarding_template")
        frappe.throw(str(e))


@frappe.whitelist()
def assign_employee_onboarding(employee, template, boarding_begins_on=None, notify=0):
    try:
        emp = frappe.get_value(
            "Employee", employee,
            ["name", "employee_name", "company", "department", "designation", "date_of_joining"],
            as_dict=True,
        )
        if not emp:
            frappe.throw(f"Employee {employee} not found.")
        tpl = frappe.get_doc("Employee Onboarding Template", template)
        doc = frappe.get_doc({
            "doctype": "Employee Onboarding",
            "employee": emp.name,
            "employee_name": emp.employee_name,
            "employee_onboarding_template": template,
            "company": emp.company or tpl.company or _get_company(),
            "department": emp.department or tpl.department,
            "designation": emp.designation or tpl.designation,
            "date_of_joining": emp.date_of_joining or boarding_begins_on or nowdate(),
            "boarding_begins_on": boarding_begins_on or emp.date_of_joining or nowdate(),
            "boarding_status": "Pending",
            "notify_users_by_email": cint(notify),
        })
        for a in tpl.get("activities", []):
            doc.append("activities", {
                "activity_name": a.activity_name,
                "role": a.role,
                "user": a.user,
                "required_for_employee_creation": a.required_for_employee_creation,
                "description": a.description,
                "task_weight": a.task_weight,
                "begin_on": a.begin_on,
                "duration": a.duration,
            })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True, ignore_mandatory=True)
        return {"name": doc.name, "employee": doc.employee, "employee_name": doc.employee_name}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: assign_employee_onboarding")
        frappe.throw(str(e))


@frappe.whitelist()
def get_asset_assignment_form(asset_name):
    try:
        asset = frappe.db.sql("""
            SELECT a.name, a.asset_name, a.asset_category, a.company, a.status,
                   a.purchase_date, a.gross_purchase_amount, a.location, a.custodian,
                   GREATEST(a.gross_purchase_amount - a.value_after_depreciation, 0)
                       AS accumulated_depreciation_amount,
                   a.value_after_depreciation AS net_asset_value
            FROM `tabAsset` a
            WHERE a.name=%(asset)s
            LIMIT 1
        """, {"asset": asset_name}, as_dict=True)
        if not asset:
            frappe.throw(f"Asset {asset_name} not found.")
        asset = asset[0]
        employee = {}
        if asset.custodian:
            employee = frappe.get_value(
                "Employee", asset.custodian,
                ["name", "employee_name", "department", "designation", "company_email", "cell_number"],
                as_dict=True,
            ) or {}
        company = frappe.get_value("Company", asset.company or _get_company(),
                                   ["name", "company_name", "email", "phone_no"], as_dict=True) or {}
        return {"asset": asset, "employee": employee, "company": company, "issue_date": nowdate()}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_asset_assignment_form")
        frappe.throw(str(e))


def _ensure_location(location_name):
    location_name = cstr(location_name).strip()
    if not location_name:
        return None
    if frappe.db.exists("Location", location_name):
        return location_name
    existing = frappe.db.get_value("Location", {"location_name": location_name}, "name")
    if existing:
        return existing
    doc = frappe.get_doc({"doctype": "Location", "location_name": location_name})
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist()
def move_asset_custody(asset_name, mode, from_employee=None, to_employee=None,
                       target_location=None, transaction_date=None, notes=None):
    try:
        asset = frappe.get_value("Asset", asset_name,
                                 ["name", "asset_name", "company", "location", "custodian"], as_dict=True)
        if not asset:
            frappe.throw(f"Asset {asset_name} not found.")
        mode = cstr(mode).lower()
        transaction_date = transaction_date or nowdate()
        if mode == "return":
            purpose = "Receipt"
            if not from_employee:
                from_employee = asset.custodian
            target_location = _ensure_location(target_location or asset.location or "IT Store")
            item = {
                "asset": asset.name,
                "asset_name": asset.asset_name,
                "source_location": asset.location,
                "target_location": target_location,
                "from_employee": from_employee,
                "company": asset.company or _get_company(),
            }
        else:
            purpose = "Issue"
            if not to_employee:
                frappe.throw("Employee receiving the asset is required.")
            item = {
                "asset": asset.name,
                "asset_name": asset.asset_name,
                "source_location": asset.location,
                "target_location": _ensure_location(target_location) if target_location else None,
                "from_employee": from_employee or asset.custodian,
                "to_employee": to_employee,
                "company": asset.company or _get_company(),
            }
        doc = frappe.get_doc({
            "doctype": "Asset Movement",
            "company": asset.company or _get_company(),
            "purpose": purpose,
            "transaction_date": transaction_date,
            "assets": [item],
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return {"name": doc.name, "purpose": doc.purpose, "asset": asset.name}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: move_asset_custody")
        frappe.throw(str(e))

@frappe.whitelist()
def enable_asset_depreciation(asset_name=None):
    """Turn on the standard depreciation schedule for existing assets that don't have one.

    Submitted assets are amended (cancel → re-submit) so ERPNext generates the schedule.
    Pass an asset name to process one, or omit to backfill every eligible asset."""
    try:
        company = _get_company()
        if asset_name:
            targets = [asset_name]
        else:
            targets = frappe.get_all("Asset",
                filters={"docstatus": ["<", 2], "calculate_depreciation": 0},
                pluck="name")

        done, skipped, errors = [], [], []
        for nm in targets:
            try:
                doc = frappe.get_doc("Asset", nm)
                if doc.calculate_depreciation:
                    skipped.append(nm)
                    continue
                # Make sure the category carries depreciation accounts + policy.
                if not _ensure_asset_category(doc.asset_category, company):
                    errors.append({"asset": nm, "error": "No depreciation accounts configured for company."})
                    continue
                pol = _depreciation_policy(doc.asset_category)

                def _configure(target):
                    target.calculate_depreciation = 1
                    target.set("finance_books", [])
                    target.append("finance_books", {
                        "depreciation_method": "Straight Line",
                        "total_number_of_depreciations": pol["years"],
                        "frequency_of_depreciation": 12,
                        "rate_of_depreciation": pol["rate"],
                        "depreciation_start_date": target.available_for_use_date or target.purchase_date,
                    })
                    target.flags.ignore_permissions = True

                if doc.docstatus == 1:
                    doc.flags.ignore_permissions = True
                    doc.cancel()
                    amended = frappe.copy_doc(doc)
                    amended.amended_from = doc.name
                    amended.docstatus = 0
                    _configure(amended)
                    amended.insert(ignore_permissions=True)
                    amended.submit()
                    done.append(amended.name)
                else:
                    _configure(doc)
                    doc.save(ignore_permissions=True)
                    doc.submit()
                    done.append(doc.name)
            except Exception as ie:
                errors.append({"asset": nm, "error": str(ie)})
        frappe.db.commit()
        return {"done": done, "done_count": len(done),
                "skipped_count": len(skipped), "errors": errors, "error_count": len(errors)}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: enable_asset_depreciation")
        frappe.throw(str(e))


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


# ── Item → Project automation custom fields ──
def _ensure_item_project_fields():
    """Idempotently add the Item fields that drive auto project/task creation."""
    if frappe.db.exists("Custom Field", "Item-custom_auto_create_project"):
        return
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({
        "Item": [
            {"fieldname": "custom_project_section", "label": "Project Automation",
             "fieldtype": "Section Break", "insert_after": "description", "collapsible": 1},
            {"fieldname": "custom_auto_create_project", "label": "Auto-create Project & Tasks on Sales Order",
             "fieldtype": "Check", "insert_after": "custom_project_section"},
            {"fieldname": "custom_project_template", "label": "Project Template",
             "fieldtype": "Link", "options": "Project Template",
             "insert_after": "custom_auto_create_project",
             "depends_on": "eval:doc.custom_auto_create_project",
             "mandatory_depends_on": "eval:doc.custom_auto_create_project"},
        ]
    }, ignore_validate=True)
    frappe.db.commit()


@frappe.whitelist()
def setup_item_project_fields():
    _ensure_item_project_fields()
    return {"ok": True}


@frappe.whitelist()
def get_project_templates():
    try:
        return frappe.get_all("Project Template", fields=["name"], order_by="name", limit=100)
    except Exception:
        return []


@frappe.whitelist(methods=["POST"])
def create_stock_item(item_code, item_name, item_group, stock_uom,
                      valuation_rate=0, safety_stock=0, description=None,
                      project_template=None, auto_create_project=0):
    _require_stock_permission("create")
    if frappe.db.exists("Item", item_code):
        frappe.throw(f"Item {item_code} already exists.")
    _ensure_item_project_fields()
    auto = int(auto_create_project or 0)
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
        "custom_auto_create_project": auto,
        "custom_project_template": project_template if auto else None,
    })
    doc.insert()
    return {"name": doc.name, "item_name": doc.item_name}


def make_projects_from_sales_order(doc, method=None):
    """For each Sales Order item whose Item has auto-create enabled + a Project Template,
    create a Project (which auto-generates the template's tasks). Idempotent per SO+template.
    Used both directly after portal SO creation and as a Sales Order doc_event."""
    try:
        if isinstance(doc, str):
            doc = frappe.get_doc("Sales Order", doc)
        _ensure_item_project_fields()
        created, seen = [], set()
        for it in doc.items:
            cfg = frappe.db.get_value("Item", it.item_code,
                ["custom_auto_create_project", "custom_project_template"], as_dict=True)
            if not cfg or not cfg.custom_auto_create_project or not cfg.custom_project_template:
                continue
            tmpl = cfg.custom_project_template
            if tmpl in seen:
                continue
            seen.add(tmpl)
            pname = f"{doc.name} · {tmpl}"
            if frappe.db.exists("Project", {"project_name": pname}):
                continue
            proj = frappe.get_doc({
                "doctype": "Project",
                "project_name": pname,
                "project_template": tmpl,
                "customer": doc.customer,
                "company": doc.company,
                "sales_order": doc.name,
                "expected_start_date": nowdate(),
            })
            proj.flags.ignore_permissions = True
            proj.insert(ignore_permissions=True)
            # Tasks are copied from the template on the next save (ERPNext guards the first insert).
            proj.save(ignore_permissions=True)
            created.append(proj.name)
        if created:
            frappe.db.commit()
        return created
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: make_projects_from_sales_order")
        return []


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


def _cbuae_aed_rate(currency):
    """AED value for 1 unit of currency from UAE Central Bank feed."""
    currency = cstr(currency).upper()
    if currency == "AED":
        return 1.0
    try:
        import requests
        resp = requests.get(
            "https://www.centralbank.ae/umbraco/Surface/Exchange/GetExchangeRateAllCurrencies",
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json() or []
        for row in rows:
            code = cstr(row.get("CurrencyCode") or row.get("currencyCode")).upper()
            if code == currency:
                raw = flt(row.get("Rate") or row.get("rateValue") or row.get("rate"))
                return raw / 100 if raw > 20 else raw
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Portal: CBUAE exchange rate")
    return None


@frappe.whitelist()
def get_uae_central_bank_exchange_rate(currency, target_currency=None, transaction_date=None, company=None):
    """Return conversion rate for 1 `currency` into `target_currency`.

    CBUAE publishes AED rates; non-AED cross rates are derived through AED.
    Falls back to ERPNext's configured exchange-rate utility when the live feed
    is unavailable.
    """
    try:
        if not company:
            company = _get_company()
        source = cstr(currency or "AED").upper()
        target = cstr(target_currency or frappe.get_value("Company", company, "default_currency") or "AED").upper()
        on_date = transaction_date or nowdate()
        if source == target:
            return {"rate": 1.0, "source": "Company Currency", "as_of": on_date}

        rate = None
        source_name = "UAE Central Bank"
        if target == "AED":
            rate = _cbuae_aed_rate(source)
        elif source == "AED":
            target_aed = _cbuae_aed_rate(target)
            rate = (1 / target_aed) if target_aed else None
        else:
            source_aed = _cbuae_aed_rate(source)
            target_aed = _cbuae_aed_rate(target)
            rate = (source_aed / target_aed) if source_aed and target_aed else None

        if not rate:
            rate = _portal_fx(source, target, on_date)
            source_name = "ERPNext Exchange Rate"

        return {"rate": flt(rate) or 1.0, "source": source_name, "as_of": on_date,
                "currency": source, "target_currency": target}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_uae_central_bank_exchange_rate")
        frappe.throw(str(e))


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
        created, skipped, created_names = 0, 0, []
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
            created_names.append(doc.name)
        auto_result = _auto_reconcile_bank_transactions(bank_account, created_names)
        frappe.db.commit()
        return {"created": created, "skipped": skipped, "auto_matched": auto_result["matched"],
                "needs_review": created - auto_result["matched"], "matches": auto_result["matches"], "ok": True}
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


def _match_text(value):
    import re
    return re.sub(r"[^a-z0-9]", "", cstr(value).lower())


def _apply_bank_payment_match(bt, payment, amount):
    for row in bt.get("payment_entries", []):
        if row.payment_document == "Payment Entry" and row.payment_entry == payment:
            return
    bt.append("payment_entries", {"payment_document":"Payment Entry", "payment_entry":payment,
                                   "allocated_amount":amount})
    bt.flags.ignore_permissions = True
    bt.save(ignore_permissions=True)
    frappe.db.set_value("Payment Entry", payment, "clearance_date", bt.date)


def _auto_reconcile_bank_transactions(bank_account, transaction_names=None, dry_run=False):
    """Auto-match only unique, high-confidence, full-amount Payment Entries."""
    from frappe.utils import date_diff

    gl_account = frappe.db.get_value("Bank Account", bank_account, "account")
    company = frappe.db.get_value("Bank Account", bank_account, "company") or _get_company()
    if not gl_account:
        return {"matched":0, "matches":[]}
    filters = {"bank_account":bank_account, "docstatus":1,
               "status":["in", ["Pending", "Unreconciled"]]}
    if transaction_names:
        filters["name"] = ["in", transaction_names]
    transactions = frappe.get_all("Bank Transaction", filters=filters,
        fields=["name","date","deposit","withdrawal","currency","description",
                "reference_number","bank_party_name"], order_by="date asc", limit=500)
    payments = frappe.db.sql("""
        SELECT pe.name, pe.posting_date, pe.payment_type, pe.party, pe.party_name,
               pe.reference_no, pe.remarks,
               CASE WHEN pe.payment_type='Receive' THEN pe.received_amount ELSE pe.paid_amount END AS bank_amount,
               CASE WHEN pe.payment_type='Receive' THEN pe.paid_to_account_currency ELSE pe.paid_from_account_currency END AS currency
        FROM `tabPayment Entry` pe
        WHERE pe.docstatus=1 AND pe.company=%(company)s AND pe.clearance_date IS NULL
          AND ((pe.payment_type='Receive' AND pe.paid_to=%(account)s)
            OR (pe.payment_type='Pay' AND pe.paid_from=%(account)s))
        ORDER BY pe.posting_date ASC
    """, {"company":company, "account":gl_account}, as_dict=True)
    used, matches = set(), []
    for txn in transactions:
        amount = flt(txn.deposit) or flt(txn.withdrawal)
        direction = "Receive" if flt(txn.deposit) > 0 else "Pay"
        txn_ref = _match_text(txn.reference_number)
        txn_text = _match_text(" ".join(filter(None, [txn.reference_number, txn.description, txn.bank_party_name])))
        txn_party = _match_text(txn.bank_party_name)
        candidates = []
        for payment in payments:
            if payment.name in used or payment.payment_type != direction:
                continue
            if cstr(payment.currency) != cstr(txn.currency) or abs(flt(payment.bank_amount)-amount) > 0.01:
                continue
            days = abs(date_diff(txn.date, payment.posting_date))
            pay_ref = _match_text(payment.reference_no)
            pay_party = _match_text(payment.party_name or payment.party)
            ref_match = bool(pay_ref and txn_ref and pay_ref == txn_ref)
            id_match = _match_text(payment.name) in txn_text
            party_match = bool(pay_party and txn_party and (pay_party in txn_party or txn_party in pay_party))
            if not (ref_match or id_match or (party_match and days <= 5)):
                continue
            score = 100 + (80 if ref_match else 0) + (70 if id_match else 0) + (35 if party_match else 0) + max(0, 20-days*4)
            candidates.append((score, payment, {"reference":ref_match or id_match, "party":party_match, "date_days":days}))
        candidates.sort(key=lambda row: row[0], reverse=True)
        if not candidates or (len(candidates)>1 and candidates[0][0]-candidates[1][0] < 20):
            continue
        score, payment, reasons = candidates[0]
        if not dry_run:
            bt = frappe.get_doc("Bank Transaction", txn.name)
            _apply_bank_payment_match(bt, payment.name, amount)
        used.add(payment.name)
        matches.append({"bank_transaction":txn.name, "payment_entry":payment.name,
                        "amount":amount, "score":score, "reasons":reasons})
    return {"matched":len(matches), "matches":matches}


@frappe.whitelist()
def auto_reconcile_bank_transactions(bank_account, from_date=None, to_date=None):
    filters = {"bank_account":bank_account, "docstatus":1,
               "status":["in", ["Pending", "Unreconciled"]]}
    if from_date: filters["date"] = [">=", from_date]
    names = frappe.get_all("Bank Transaction", filters=filters, pluck="name", limit=500)
    if to_date:
        names = frappe.get_all("Bank Transaction", filters={**filters, "date":["between", [from_date or "1900-01-01", to_date]]}, pluck="name", limit=500)
    result = _auto_reconcile_bank_transactions(bank_account, names)
    frappe.db.commit()
    return result


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


@frappe.whitelist()
def get_account_meta(company=None):
    """Group accounts (possible parents) + account-type options for the COA form."""
    if not company:
        company = _get_company()
    groups = frappe.get_all("Account", filters={"company": company, "is_group": 1},
                            fields=["name", "account_name", "root_type"], order_by="lft")
    account_types = frappe.get_meta("Account").get_field("account_type").options or ""
    return {
        "groups": groups,
        "account_types": [t for t in account_types.split("\n") if t.strip()],
        "root_types": ["Asset", "Liability", "Equity", "Income", "Expense"],
    }


@frappe.whitelist()
def create_account(account_name, parent_account, company=None, is_group=0,
                   account_type=None, account_number=None, root_type=None):
    """Create a ledger or group account under a parent."""
    try:
        if frappe.session.user in ("Guest", None, ""):
            frappe.throw("You must be signed in.")
        if not company:
            company = _get_company()
        if not parent_account:
            frappe.throw("Please choose a parent account.")
        doc = frappe.get_doc({
            "doctype": "Account",
            "account_name": cstr(account_name).strip(),
            "parent_account": parent_account,
            "company": company,
            "is_group": cint(is_group),
            "account_number": cstr(account_number).strip() or None,
            "account_type": account_type or None,
            "root_type": root_type or None,
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        return {"name": doc.name, "account_name": doc.account_name}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: create_account")
        frappe.throw(str(e))


@frappe.whitelist()
def update_account(name, account_name=None, account_number=None, account_type=None):
    """Edit a limited, safe set of fields on an existing account."""
    try:
        if frappe.session.user in ("Guest", None, ""):
            frappe.throw("You must be signed in.")
        doc = frappe.get_doc("Account", name)
        if account_name:
            # ERPNext renames the Account document automatically when account_name changes.
            doc.account_name = cstr(account_name).strip()
        if account_number is not None:
            doc.account_number = cstr(account_number).strip() or None
        if account_type is not None and not doc.is_group:
            doc.account_type = account_type or None
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        return {"ok": True, "name": doc.name}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: update_account")
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


# ══════════════════════════════════════════════════════════════════
#  LEAVE POLICY · BULK ASSIGNMENT · UAE LABOUR LAW · PROBATION ACCRUAL
# ══════════════════════════════════════════════════════════════════

# UAE Federal Decree-Law No. 33 of 2021 statutory leave entitlements.
# UAE sick leave (after probation) is capped at 90 days/year but paid in tiers:
#   first 15 days full pay · next 30 days half pay · final 45 days unpaid.
# ERPNext models pay per leave type, so the 90 days are split into three tiers
# using `fraction` (fraction of daily salary paid) and `lwp` (leave without pay).
UAE_LEAVE_TYPES = [
    {"name": "Annual Leave",            "max": 30, "carry": 1, "lwp": 0},
    {"name": "Sick Leave (Full Pay)",   "max": 15, "carry": 0, "lwp": 0, "fraction": 1.0},
    {"name": "Sick Leave (Half Pay)",   "max": 30, "carry": 0, "lwp": 0, "fraction": 0.5},
    {"name": "Sick Leave (Unpaid)",     "max": 45, "carry": 0, "lwp": 1, "fraction": 0.0},
    {"name": "Maternity Leave",         "max": 60, "carry": 0, "lwp": 0},
    {"name": "Parental Leave",          "max": 5,  "carry": 0, "lwp": 0},
    {"name": "Bereavement Leave",       "max": 5,  "carry": 0, "lwp": 0},
    {"name": "Hajj Leave",              "max": 30, "carry": 0, "lwp": 1},
    {"name": "Study Leave",             "max": 10, "carry": 0, "lwp": 0},
]
UAE_POLICY_TITLE = "UAE Labour Law Policy"


def _ensure_leave_type(name, max_leaves=0, carry=0, lwp=0, fraction=None):
    if frappe.db.exists("Leave Type", name):
        # Raise the cap if an existing type allows fewer days than we want to allocate,
        # otherwise Leave Policy validation rejects the allocation.
        if max_leaves:
            cur = flt(frappe.db.get_value("Leave Type", name, "max_leaves_allowed"))
            if cur and cur < max_leaves:
                frappe.db.set_value("Leave Type", name, "max_leaves_allowed", max_leaves)
        return name
    lt = frappe.get_doc({
        "doctype": "Leave Type",
        "leave_type_name": name,
        "max_leaves_allowed": max_leaves,
        "is_carry_forward": carry,
        "is_lwp": lwp,
        "include_holiday": 0,
    })
    if fraction is not None and not lwp:
        lt.fraction_of_daily_salary_per_leave = fraction
    lt.flags.ignore_permissions = True
    lt.insert(ignore_permissions=True)
    return lt.name


@frappe.whitelist()
def setup_uae_leave_policy():
    """Create the UAE statutory leave types and a ready-to-assign Leave Policy."""
    try:
        for t in UAE_LEAVE_TYPES:
            _ensure_leave_type(t["name"], t["max"], t["carry"], t["lwp"], t.get("fraction"))

        if frappe.db.exists("Leave Policy", {"title": UAE_POLICY_TITLE}):
            name = frappe.db.get_value("Leave Policy", {"title": UAE_POLICY_TITLE}, "name")
            return {"name": name, "created": False, "message": "UAE policy already exists."}

        policy = frappe.get_doc({
            "doctype": "Leave Policy",
            "title": UAE_POLICY_TITLE,
            "leave_policy_details": [
                {"leave_type": t["name"], "annual_allocation": t["max"]}
                for t in UAE_LEAVE_TYPES
            ],
        })
        policy.flags.ignore_permissions = True
        policy.insert(ignore_permissions=True)
        frappe.db.commit()
        return {"name": policy.name, "created": True, "message": "UAE Labour Law policy created."}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: setup_uae_leave_policy")
        frappe.throw(str(e))


@frappe.whitelist()
def get_leave_policies():
    """Return all leave policies with their per-type allocation details."""
    try:
        policies = frappe.get_all("Leave Policy", fields=["name", "title"],
                                  order_by="modified desc", limit=100)
        for p in policies:
            details = frappe.get_all("Leave Policy Detail",
                filters={"parent": p["name"]},
                fields=["leave_type", "annual_allocation"], order_by="idx")
            p["details"] = details
            p["total_allocation"] = sum(flt(d["annual_allocation"]) for d in details)
            p["assigned_count"] = frappe.db.count("Leave Policy Assignment",
                {"leave_policy": p["name"], "docstatus": 1})
        return policies
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_leave_policies")
        frappe.throw(str(e))


@frappe.whitelist()
def create_leave_policy(title, details):
    """Create a custom leave policy. `details` = JSON list of {leave_type, annual_allocation}."""
    import json
    try:
        rows = json.loads(details) if isinstance(details, str) else details
        if not title or not rows:
            frappe.throw("Title and at least one leave type are required.")
        for r in rows:
            _ensure_leave_type(r["leave_type"], flt(r.get("annual_allocation")))
        policy = frappe.get_doc({
            "doctype": "Leave Policy",
            "title": title,
            "leave_policy_details": [
                {"leave_type": r["leave_type"], "annual_allocation": flt(r.get("annual_allocation"))}
                for r in rows
            ],
        })
        policy.flags.ignore_permissions = True
        policy.insert(ignore_permissions=True)
        frappe.db.commit()
        return {"name": policy.name, "title": policy.title}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: create_leave_policy")
        frappe.throw(str(e))


@frappe.whitelist()
def get_leave_policy_assignments(limit=200):
    try:
        return frappe.get_all("Leave Policy Assignment",
            filters={"docstatus": ["!=", 2]},
            fields=["name", "employee", "employee_name", "leave_policy",
                    "effective_from", "effective_to", "leaves_allocated", "docstatus"],
            order_by="creation desc", limit=limit)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_leave_policy_assignments")
        return []


@frappe.whitelist()
def bulk_assign_leave_policy(leave_policy, employees, effective_from=None, effective_to=None):
    """Assign one leave policy to many employees at once and grant their allocations.

    `employees` = JSON list of employee ids (or comma string)."""
    import json
    from frappe.utils import getdate, add_years, add_days
    try:
        if isinstance(employees, str):
            try:
                emp_list = json.loads(employees)
            except Exception:
                emp_list = [e.strip() for e in employees.split(",") if e.strip()]
        else:
            emp_list = employees or []
        if not leave_policy or not emp_list:
            frappe.throw("A policy and at least one employee are required.")

        if not effective_from:
            effective_from = nowdate()
        if not effective_to:
            effective_to = add_days(add_years(getdate(effective_from), 1), -1).strftime("%Y-%m-%d")

        assigned, skipped, errors = [], [], []
        for emp in emp_list:
            try:
                exists = frappe.db.exists("Leave Policy Assignment", {
                    "employee": emp, "leave_policy": leave_policy,
                    "effective_from": effective_from, "docstatus": ["!=", 2],
                })
                if exists:
                    skipped.append(emp)
                    continue
                lpa = frappe.get_doc({
                    "doctype": "Leave Policy Assignment",
                    "employee": emp,
                    "leave_policy": leave_policy,
                    "assignment_based_on": "",
                    "effective_from": effective_from,
                    "effective_to": effective_to,
                })
                lpa.flags.ignore_permissions = True
                lpa.insert(ignore_permissions=True)
                lpa.submit()
                assigned.append(emp)
            except Exception as ie:
                errors.append({"employee": emp, "error": str(ie)})
        frappe.db.commit()
        return {"assigned": assigned, "skipped": skipped, "errors": errors,
                "assigned_count": len(assigned), "skipped_count": len(skipped),
                "error_count": len(errors)}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: bulk_assign_leave_policy")
        frappe.throw(str(e))


def _months_of_service(doj):
    from frappe.utils import getdate
    if not doj:
        return 0
    d = getdate(doj)
    t = getdate(nowdate())
    return (t.year - d.year) * 12 + (t.month - d.month) - (1 if t.day < d.day else 0)


UAE_PROBATION_RATE = 2  # working days accrued per month of service (6–12 months)


def _probation_accrued_days(employee):
    """Days already auto-accrued for this employee via probation accrual (submitted)."""
    rows = frappe.db.sql("""
        SELECT COALESCE(SUM(new_leaves_allocated), 0) AS d
        FROM `tabLeave Allocation`
        WHERE employee=%(e)s AND leave_type='Annual Leave'
          AND docstatus=1 AND description LIKE 'UAE Probation Accrual%%'
    """, {"e": employee}, as_dict=True)
    return flt(rows[0].d) if rows else 0


@frappe.whitelist()
def get_probation_accrual_preview():
    """Active employees with 6–12 months service — auto-computed accrual figures.

    owed = 2 days × months of service · already = previously accrued · to_grant = owed − already."""
    try:
        emps = frappe.get_all("Employee", filters={"status": "Active"},
            fields=["name", "employee_name", "date_of_joining", "department"], limit=1000)
        out = []
        for e in emps:
            m = _months_of_service(e.date_of_joining)
            if 6 <= m < 12:
                owed = UAE_PROBATION_RATE * m
                already = _probation_accrued_days(e.name)
                to_grant = max(0, owed - already)
                out.append({"employee": e.name, "employee_name": e.employee_name,
                            "department": e.department, "months": m,
                            "date_of_joining": str(e.date_of_joining or ""),
                            "owed": owed, "accrued": already, "to_grant": to_grant,
                            "accrued_this_month": to_grant <= 0})
        return out
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_probation_accrual_preview")
        return []


@frappe.whitelist()
def accrue_uae_probation_leave():
    """Grant 2 days Annual Leave for the current month to each employee with 6–12 months
    of service (UAE law: 2 working days/month before completing one year).
    Idempotent per calendar month via a description marker. Safe to run monthly."""
    from frappe.utils import getdate
    try:
        _ensure_leave_type("Annual Leave", 30, 1, 0)
        emps = frappe.get_all("Employee", filters={"status": "Active"},
            fields=["name", "employee_name", "date_of_joining"], limit=2000)
        today = getdate(nowdate())
        ym = today.strftime("%Y-%m")
        year_end = f"{today.year}-12-31"
        granted = []
        total_days = 0
        for e in emps:
            m = _months_of_service(e.date_of_joining)
            if not (6 <= m < 12):
                continue
            # Auto-calculate: owed = 2 days × months of service, grant only the shortfall.
            owed = UAE_PROBATION_RATE * m
            already = _probation_accrued_days(e.name)
            to_grant = owed - already
            if to_grant <= 0:
                continue
            try:
                alloc = frappe.get_doc({
                    "doctype": "Leave Allocation",
                    "employee": e.name,
                    "leave_type": "Annual Leave",
                    "from_date": nowdate(),
                    "to_date": year_end,
                    "new_leaves_allocated": to_grant,
                    "description": f"UAE Probation Accrual {ym} (+{to_grant}d · total {owed}d for {m} months)",
                    "carry_forward": 0,
                })
                alloc.flags.ignore_permissions = True
                alloc.insert(ignore_permissions=True)
                alloc.submit()
                total_days += to_grant
                granted.append({"employee": e.name, "employee_name": e.employee_name,
                                "months": m, "days": to_grant, "total": owed})
            except Exception as ie:
                frappe.log_error(str(ie), "Portal: accrue_uae_probation_leave row")
        frappe.db.commit()
        return {"granted_count": len(granted), "granted": granted,
                "total_days": total_days, "month": ym}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: accrue_uae_probation_leave")
        frappe.throw(str(e))


# ══════════════════════════════════════════════════════════════════
#  CREDIT NOTES (Sales returns) · DEBIT NOTES (Purchase returns)
# ══════════════════════════════════════════════════════════════════

@frappe.whitelist()
def get_returnable_sales_invoices(company=None):
    """Submitted, non-return sales invoices that a credit note can be raised against."""
    company = company or _get_company()
    return frappe.get_all("Sales Invoice",
        filters={"docstatus": 1, "is_return": 0, "company": company},
        fields=["name", "customer", "customer_name", "posting_date", "grand_total", "currency"],
        order_by="posting_date desc", limit=200)


@frappe.whitelist()
def get_returnable_purchase_invoices(company=None):
    """Submitted, non-return purchase invoices that a debit note can be raised against."""
    company = company or _get_company()
    return frappe.get_all("Purchase Invoice",
        filters={"docstatus": 1, "is_return": 0, "company": company},
        fields=["name", "supplier", "supplier_name", "posting_date", "grand_total", "currency"],
        order_by="posting_date desc", limit=200)


@frappe.whitelist()
def get_credit_notes(company=None):
    company = company or _get_company()
    return frappe.get_all("Sales Invoice",
        filters={"is_return": 1, "company": company, "docstatus": ["<", 2]},
        fields=["name", "customer", "customer_name", "return_against", "posting_date",
                "grand_total", "currency", "status", "docstatus"],
        order_by="posting_date desc", limit=300)


@frappe.whitelist()
def get_debit_notes(company=None):
    company = company or _get_company()
    return frappe.get_all("Purchase Invoice",
        filters={"is_return": 1, "company": company, "docstatus": ["<", 2]},
        fields=["name", "supplier", "supplier_name", "return_against", "posting_date",
                "grand_total", "currency", "status", "docstatus"],
        order_by="posting_date desc", limit=300)


def _make_return_note(doctype, return_against, posting_date=None, remarks=None):
    from erpnext.controllers.sales_and_purchase_return import make_return_doc
    if not return_against:
        frappe.throw("Select the original invoice to return against.")
    if not frappe.db.exists(doctype, {"name": return_against, "docstatus": 1, "is_return": 0}):
        frappe.throw(f"{doctype} {return_against} is not a submitted, returnable invoice.")
    doc = make_return_doc(doctype, return_against)
    doc.posting_date = posting_date or nowdate()
    if doctype == "Sales Invoice":
        doc.set_posting_time = 1
    if remarks:
        doc.remarks = remarks
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    doc.submit()
    return doc


@frappe.whitelist()
def create_credit_note(return_against, posting_date=None, remarks=None):
    """Create & submit a Credit Note (sales return) against an existing Sales Invoice."""
    try:
        doc = _make_return_note("Sales Invoice", return_against, posting_date, remarks)
        return {"name": doc.name, "grand_total": doc.grand_total,
                "customer": doc.customer, "return_against": doc.return_against}
    except frappe.ValidationError as e:
        frappe.throw(str(e))
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Credit Note")
        frappe.throw(str(e))


@frappe.whitelist()
def create_debit_note(return_against, posting_date=None, remarks=None):
    """Create & submit a Debit Note (purchase return) against an existing Purchase Invoice."""
    try:
        doc = _make_return_note("Purchase Invoice", return_against, posting_date, remarks)
        return {"name": doc.name, "grand_total": doc.grand_total,
                "supplier": doc.supplier, "return_against": doc.return_against}
    except frappe.ValidationError as e:
        frappe.throw(str(e))
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: Create Debit Note")
        frappe.throw(str(e))


@frappe.whitelist()
def get_task_insights(company=None):
    """Aggregated task analytics: status mix, per-user workload, and daily throughput."""
    import json as _json
    from frappe.utils import getdate, add_days, date_diff
    company = company or _get_company()
    try:
        tasks = frappe.get_all("Task",
            filters={"company": company} if company else {},
            fields=["name", "subject", "status", "exp_end_date", "creation", "modified", "_assign"],
            limit=5000)
        today = getdate(nowdate())
        win = 30
        start = add_days(today, -win)

        status_count = {}
        overdue = 0
        assigned_recent = 0
        completed_recent = 0
        user_map = {}   # user -> {open, completed, total}
        unassigned = 0

        for t in tasks:
            st = t.status or "Open"
            status_count[st] = status_count.get(st, 0) + 1
            is_done = st in ("Completed", "Cancelled")
            if t.exp_end_date and getdate(t.exp_end_date) < today and not is_done:
                overdue += 1
            if t.creation and getdate(t.creation) >= start:
                assigned_recent += 1
            if st == "Completed" and t.modified and getdate(t.modified) >= start:
                completed_recent += 1
            assignees = []
            if t.get("_assign"):
                try:
                    assignees = _json.loads(t._assign) or []
                except Exception:
                    assignees = []
            if not assignees:
                unassigned += 1
            for u in assignees:
                m = user_map.setdefault(u, {"open": 0, "completed": 0, "total": 0})
                m["total"] += 1
                if st == "Completed":
                    m["completed"] += 1
                elif not is_done:
                    m["open"] += 1

        # attach display names
        names = {}
        if user_map:
            for r in frappe.get_all("User", filters={"name": ["in", list(user_map.keys())]},
                                    fields=["name", "full_name"]):
                names[r.name] = r.full_name
        workload = [{"user": u, "full_name": names.get(u, u),
                     "open": v["open"], "completed": v["completed"], "total": v["total"]}
                    for u, v in user_map.items()]
        workload.sort(key=lambda x: x["total"], reverse=True)

        active_users = sum(1 for v in user_map.values() if v["open"] > 0)
        total = len(tasks)
        completed = status_count.get("Completed", 0)

        return {
            "total": total,
            "completed": completed,
            "in_progress": status_count.get("Working", 0) + status_count.get("Open", 0) + status_count.get("Pending Review", 0),
            "overdue": overdue,
            "completion_rate": round(completed / total * 100) if total else 0,
            "active_users": active_users,
            "unassigned": unassigned,
            "status_breakdown": status_count,
            "avg_daily_assigned": round(assigned_recent / win, 1),
            "avg_daily_completed": round(completed_recent / win, 1),
            "window_days": win,
            "workload": workload[:10],
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_task_insights")
        return {"total": 0, "workload": [], "status_breakdown": {}}


# ══════════════════════════════════════════════════════════════════
#  TEAM: Who's In · Celebrations · Kudos · Org Chart
# ══════════════════════════════════════════════════════════════════

@frappe.whitelist()
def get_whos_in(company=None):
    """Live team presence for today from Leave, Attendance and Check-in data."""
    from frappe.utils import getdate
    try:
        emps = frappe.get_all("Employee", filters={"status": "Active"},
            fields=["name", "employee_name", "image", "department", "designation"],
            order_by="employee_name", limit=2000)
        today = nowdate()
        on_leave = set(frappe.get_all("Leave Application",
            filters={"status": "Approved", "docstatus": 1,
                     "from_date": ["<=", today], "to_date": [">=", today]}, pluck="employee"))
        att = {a.employee: a.status for a in frappe.get_all("Attendance",
            filters={"attendance_date": today, "docstatus": 1},
            fields=["employee", "status"])}
        last_in = {}
        try:
            for r in frappe.get_all("Employee Checkin",
                filters={"time": ["between", [today + " 00:00:00", today + " 23:59:59"]]},
                fields=["employee", "log_type", "time"], order_by="time desc", limit=4000):
                if r.employee not in last_in:
                    last_in[r.employee] = r
        except Exception:
            pass

        def status_for(e):
            if e.name in on_leave:
                return "On Leave"
            s = att.get(e.name)
            if s == "Work From Home":
                return "WFH"
            if s in ("Present", "Half Day"):
                return s
            if s == "Absent":
                return "Absent"
            lg = last_in.get(e.name)
            if lg and lg.log_type == "IN":
                return "Present"
            return "Out"

        members, counts = [], {"Present": 0, "WFH": 0, "On Leave": 0, "Half Day": 0, "Absent": 0, "Out": 0}
        for e in emps:
            st = status_for(e)
            counts[st] = counts.get(st, 0) + 1
            members.append({"employee": e.name, "employee_name": e.employee_name,
                            "image": e.image, "department": e.department,
                            "designation": e.designation, "status": st})
        order = {"Present": 0, "WFH": 1, "Half Day": 2, "On Leave": 3, "Absent": 4, "Out": 5}
        members.sort(key=lambda m: (order.get(m["status"], 9), m["employee_name"] or ""))
        present_like = counts["Present"] + counts["WFH"] + counts["Half Day"]
        return {"total": len(emps), "in_count": present_like, "counts": counts, "members": members}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_whos_in")
        return {"total": 0, "in_count": 0, "counts": {}, "members": []}


@frappe.whitelist()
def get_celebrations(company=None):
    """Birthdays and work anniversaries falling in the current month."""
    from frappe.utils import getdate
    try:
        today = getdate(nowdate())
        m = today.month
        emps = frappe.get_all("Employee", filters={"status": "Active"},
            fields=["name", "employee_name", "image", "department", "designation",
                    "date_of_birth", "date_of_joining"], limit=2000)
        birthdays, anniversaries = [], []
        for e in emps:
            if e.date_of_birth and getdate(e.date_of_birth).month == m:
                d = getdate(e.date_of_birth)
                birthdays.append({"employee": e.name, "employee_name": e.employee_name,
                                  "image": e.image, "department": e.department,
                                  "day": d.day, "date": d.replace(year=today.year).strftime("%Y-%m-%d"),
                                  "upcoming": d.day >= today.day})
            if e.date_of_joining and getdate(e.date_of_joining).month == m:
                d = getdate(e.date_of_joining)
                yrs = today.year - d.year
                if yrs >= 1:
                    anniversaries.append({"employee": e.name, "employee_name": e.employee_name,
                                          "image": e.image, "department": e.department,
                                          "day": d.day, "years": yrs,
                                          "date": d.replace(year=today.year).strftime("%Y-%m-%d"),
                                          "upcoming": d.day >= today.day})
        birthdays.sort(key=lambda x: x["day"])
        anniversaries.sort(key=lambda x: x["day"])
        return {"month": today.strftime("%B"), "today_day": today.day,
                "birthdays": birthdays, "anniversaries": anniversaries}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_celebrations")
        return {"birthdays": [], "anniversaries": []}


def _ensure_kudos_doctype():
    if frappe.db.exists("DocType", "Team Kudos"):
        return
    dt = frappe.get_doc({
        "doctype": "DocType", "name": "Team Kudos", "module": "Next Ai",
        "custom": 1, "autoname": "hash", "track_changes": 0,
        "fields": [
            {"fieldname": "to_employee", "label": "To Employee", "fieldtype": "Link", "options": "Employee", "in_list_view": 1, "reqd": 1},
            {"fieldname": "to_employee_name", "label": "To Employee Name", "fieldtype": "Data"},
            {"fieldname": "message", "label": "Message", "fieldtype": "Small Text", "in_list_view": 1},
            {"fieldname": "points", "label": "Points", "fieldtype": "Int", "default": "10"},
            {"fieldname": "given_by", "label": "Given By", "fieldtype": "Data"},
            {"fieldname": "given_by_name", "label": "Given By Name", "fieldtype": "Data"},
        ],
        "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
    })
    dt.flags.ignore_permissions = True
    dt.insert(ignore_permissions=True)
    frappe.db.commit()


@frappe.whitelist()
def give_kudos(to_employee, message, points=10):
    try:
        _ensure_kudos_doctype()
        if not to_employee or not (message or "").strip():
            frappe.throw("Pick a colleague and write a short message.")
        emp_name = frappe.db.get_value("Employee", to_employee, "employee_name") or to_employee
        user = frappe.session.user
        giver = frappe.db.get_value("User", user, "full_name") or user
        doc = frappe.get_doc({
            "doctype": "Team Kudos", "to_employee": to_employee, "to_employee_name": emp_name,
            "message": (message or "").strip()[:500], "points": int(points or 10),
            "given_by": user, "given_by_name": giver,
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        return {"name": doc.name}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: give_kudos")
        frappe.throw(str(e))


@frappe.whitelist()
def get_kudos_feed(limit=25):
    try:
        _ensure_kudos_doctype()
        feed = frappe.get_all("Team Kudos",
            fields=["name", "to_employee", "to_employee_name", "message", "points",
                    "given_by", "given_by_name", "creation"],
            order_by="creation desc", limit=int(limit))
        board = {}
        for r in frappe.get_all("Team Kudos", fields=["to_employee", "to_employee_name", "points"], limit=5000):
            b = board.setdefault(r.to_employee, {"employee": r.to_employee,
                                                 "employee_name": r.to_employee_name, "points": 0, "count": 0})
            b["points"] += int(r.points or 0); b["count"] += 1
        # attach images
        imgs = {e.name: e.image for e in frappe.get_all("Employee",
                filters={"name": ["in", list(board.keys())]}, fields=["name", "image"])} if board else {}
        leaderboard = sorted(board.values(), key=lambda x: x["points"], reverse=True)[:8]
        for b in leaderboard:
            b["image"] = imgs.get(b["employee"])
        return {"feed": feed, "leaderboard": leaderboard}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_kudos_feed")
        return {"feed": [], "leaderboard": []}


@frappe.whitelist()
def get_org_chart(company=None):
    """Active employees with their manager link, for building a reporting tree."""
    try:
        emps = frappe.get_all("Employee", filters={"status": "Active"},
            fields=["name", "employee_name", "image", "designation", "department", "reports_to"],
            order_by="employee_name", limit=3000)
        return emps
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_org_chart")
        return []


@frappe.whitelist()
def get_stock_insights(company=None):
    """Rich inventory analytics: balance, reorder/low-stock, fast & slow movers,
    average purchase/selling price, and warehouse-wise stock."""
    from frappe.utils import add_days
    _require_stock_permission()
    company = company or _get_company()
    try:
        start90 = add_days(nowdate(), -90)
        start180 = add_days(nowdate(), -180)
        p = {"company": company, "s90": start90, "s180": start180}

        items = frappe.db.sql("""
            SELECT i.name AS item_code, i.item_name, i.item_group, i.stock_uom,
                   i.safety_stock, i.valuation_rate,
                   IFNULL(SUM(CASE WHEN w.name IS NOT NULL THEN b.actual_qty ELSE 0 END),0) AS actual_qty,
                   IFNULL(SUM(CASE WHEN w.name IS NOT NULL THEN b.stock_value ELSE 0 END),0) AS stock_value
            FROM `tabItem` i
            LEFT JOIN `tabBin` b ON b.item_code=i.name
            LEFT JOIN `tabWarehouse` w ON w.name=b.warehouse AND w.company=%(company)s
            WHERE i.disabled=0 AND i.is_stock_item=1
            GROUP BY i.name, i.item_name, i.item_group, i.stock_uom, i.safety_stock, i.valuation_rate
            LIMIT 2000
        """, p, as_dict=True)

        out_map = {r.item_code: flt(r.qty_out) for r in frappe.db.sql("""
            SELECT sle.item_code, SUM(CASE WHEN sle.actual_qty<0 THEN -sle.actual_qty ELSE 0 END) AS qty_out
            FROM `tabStock Ledger Entry` sle
            JOIN `tabWarehouse` w ON w.name=sle.warehouse AND w.company=%(company)s
            WHERE sle.posting_date >= %(s90)s AND sle.is_cancelled=0
            GROUP BY sle.item_code
        """, p, as_dict=True)}

        sell_map = {r.item_code: flt(r.avg_sell) for r in frappe.db.sql("""
            SELECT sii.item_code, AVG(sii.base_net_rate) AS avg_sell
            FROM `tabSales Invoice Item` sii JOIN `tabSales Invoice` si ON si.name=sii.parent
            WHERE si.docstatus=1 AND si.company=%(company)s AND si.posting_date>=%(s180)s AND sii.base_net_rate>0
            GROUP BY sii.item_code
        """, p, as_dict=True)}

        buy_map = {r.item_code: flt(r.avg_buy) for r in frappe.db.sql("""
            SELECT pii.item_code, AVG(pii.base_net_rate) AS avg_buy
            FROM `tabPurchase Invoice Item` pii JOIN `tabPurchase Invoice` pi ON pi.name=pii.parent
            WHERE pi.docstatus=1 AND pi.company=%(company)s AND pi.posting_date>=%(s180)s AND pii.base_net_rate>0
            GROUP BY pii.item_code
        """, p, as_dict=True)}

        warehouses = frappe.db.sql("""
            SELECT b.warehouse, SUM(b.actual_qty) AS qty, SUM(b.stock_value) AS value
            FROM `tabBin` b JOIN `tabWarehouse` w ON w.name=b.warehouse AND w.company=%(company)s
            GROUP BY b.warehouse HAVING qty <> 0 ORDER BY value DESC LIMIT 30
        """, p, as_dict=True)

        total_qty = total_value = 0
        low_stock, reorder, fast, slow, price = [], [], [], [], []
        out_of_stock = 0
        for it in items:
            qty = flt(it.actual_qty); val = flt(it.stock_value)
            total_qty += qty; total_value += val
            qty_out = out_map.get(it.item_code, 0)
            it["qty_out_90"] = qty_out
            if qty <= 0:
                out_of_stock += 1
            if flt(it.safety_stock) > 0 and qty <= flt(it.safety_stock):
                reorder.append({**it, "shortfall": flt(it.safety_stock) - qty})
            avg_buy = buy_map.get(it.item_code) or flt(it.valuation_rate)
            avg_sell = sell_map.get(it.item_code, 0)
            if avg_sell or avg_buy:
                margin = round((avg_sell - avg_buy) / avg_sell * 100) if avg_sell else 0
                price.append({"item_code": it.item_code, "item_name": it.item_name,
                              "avg_purchase": round(avg_buy, 2), "avg_selling": round(avg_sell, 2),
                              "margin_pct": margin, "valuation_rate": flt(it.valuation_rate)})

        movers = sorted(items, key=lambda x: x.get("qty_out_90", 0), reverse=True)
        fast = [m for m in movers if m.get("qty_out_90", 0) > 0][:10]
        slow = sorted([m for m in items if m.get("qty_out_90", 0) == 0 and flt(m.actual_qty) > 0],
                      key=lambda x: flt(x.stock_value), reverse=True)[:10]
        reorder.sort(key=lambda x: x["shortfall"], reverse=True)
        price.sort(key=lambda x: x["margin_pct"], reverse=True)

        return {
            "summary": {"total_items": len(items), "total_qty": total_qty,
                        "total_value": total_value, "reorder_count": len(reorder),
                        "out_of_stock": out_of_stock, "warehouse_count": len(warehouses)},
            "fast_moving": fast, "slow_moving": slow, "reorder": reorder[:25],
            "warehouses": warehouses, "price": price[:25],
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_stock_insights")
        frappe.throw(str(e))


# ══════════════════════════════════════════════════════════════════
#  SETTINGS: Portal Users & Access (creation + roles/permissions)
# ══════════════════════════════════════════════════════════════════

# Roles most relevant to the firm portal modules.
PORTAL_ROLE_CHOICES = [
    {"role": "Accounts User", "group": "Accounting"},
    {"role": "Accounts Manager", "group": "Accounting"},
    {"role": "Sales User", "group": "Sales"},
    {"role": "Sales Manager", "group": "Sales"},
    {"role": "Purchase User", "group": "Buying"},
    {"role": "Purchase Manager", "group": "Buying"},
    {"role": "Stock User", "group": "Stock"},
    {"role": "Stock Manager", "group": "Stock"},
    {"role": "HR User", "group": "HR"},
    {"role": "HR Manager", "group": "HR"},
    {"role": "Projects User", "group": "Projects"},
    {"role": "Projects Manager", "group": "Projects"},
    {"role": "Employee", "group": "General"},
    {"role": "System Manager", "group": "Administration"},
]


def _require_user_admin():
    roles = frappe.get_roles()
    if "System Manager" not in roles:
        frappe.throw("Only System Managers can manage portal users and access.")


@frappe.whitelist()
def get_portal_access():
    _require_user_admin()
    users = frappe.get_all("User",
        filters={"user_type": "System User", "name": ["not in", ["Administrator", "Guest"]]},
        fields=["name", "full_name", "enabled", "user_image", "last_active"],
        order_by="full_name asc", limit=500)
    role_map = {}
    for r in frappe.get_all("Has Role", fields=["parent", "role"], limit=10000):
        role_map.setdefault(r.parent, []).append(r.role)
    allowed = {c["role"] for c in PORTAL_ROLE_CHOICES}
    for u in users:
        u["roles"] = sorted(r for r in role_map.get(u.name, []) if r in allowed)
        u["all_roles_count"] = len(role_map.get(u.name, []))
    return {"users": users, "roles": PORTAL_ROLE_CHOICES}


@frappe.whitelist()
def create_portal_user(email, first_name, last_name=None, roles=None, send_welcome=0):
    import json as _json
    _require_user_admin()
    if not email or not first_name:
        frappe.throw("Email and first name are required.")
    if frappe.db.exists("User", email):
        frappe.throw(f"User {email} already exists.")
    role_list = _json.loads(roles) if isinstance(roles, str) else (roles or [])
    role_list = [r for r in role_list if r in {c["role"] for c in PORTAL_ROLE_CHOICES}]
    try:
        u = frappe.get_doc({
            "doctype": "User", "email": email,
            "first_name": first_name, "last_name": last_name or "",
            "user_type": "System User", "send_welcome_email": int(send_welcome or 0),
        })
        u.flags.ignore_permissions = True
        u.insert(ignore_permissions=True)
        if role_list:
            u.add_roles(*role_list)
        frappe.db.commit()
        return {"name": u.name, "full_name": u.full_name, "roles": role_list}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: create_portal_user")
        frappe.throw(str(e))


@frappe.whitelist()
def set_portal_user_roles(user, roles):
    import json as _json
    _require_user_admin()
    if user == "Administrator":
        frappe.throw("The Administrator account cannot be edited here.")
    target = set(_json.loads(roles) if isinstance(roles, str) else (roles or []))
    allowed = {c["role"] for c in PORTAL_ROLE_CHOICES}
    target = {r for r in target if r in allowed}
    try:
        u = frappe.get_doc("User", user)
        current = {r.role for r in u.roles if r.role in allowed}
        to_add, to_remove = target - current, current - target
        if to_add:
            u.add_roles(*to_add)
        if to_remove:
            u.remove_roles(*to_remove)
        frappe.db.commit()
        return {"user": user, "roles": sorted(target)}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: set_portal_user_roles")
        frappe.throw(str(e))


@frappe.whitelist()
def set_portal_user_enabled(user, enabled):
    _require_user_admin()
    if user in ("Administrator", frappe.session.user):
        frappe.throw("You cannot disable this account.")
    frappe.db.set_value("User", user, "enabled", int(enabled))
    frappe.db.commit()
    return {"user": user, "enabled": int(enabled)}


# ══════════════════════════════════════════════════════════════════
#  PROJECTS & TASKS: My Work · Timesheets · Profitability
# ══════════════════════════════════════════════════════════════════

@frappe.whitelist()
def get_my_work(user=None):
    """The current user's open tasks, bucketed by due date."""
    from frappe.utils import getdate
    user = user or frappe.session.user
    try:
        tasks = frappe.get_all("Task",
            filters=[["_assign", "like", f"%{user}%"], ["status", "not in", ["Completed", "Cancelled"]]],
            fields=["name", "subject", "status", "priority", "exp_end_date", "progress", "project"],
            order_by="exp_end_date asc", limit=500)
        today = getdate(nowdate())
        buckets = {"overdue": [], "today": [], "upcoming": [], "no_date": []}
        proj_names = {}
        for t in tasks:
            if t.project and t.project not in proj_names:
                proj_names[t.project] = frappe.db.get_value("Project", t.project, "project_name") or t.project
            t["project_name"] = proj_names.get(t.project, "")
            if not t.exp_end_date:
                buckets["no_date"].append(t)
            elif getdate(t.exp_end_date) < today:
                buckets["overdue"].append(t)
            elif getdate(t.exp_end_date) == today:
                buckets["today"].append(t)
            else:
                buckets["upcoming"].append(t)
        return {"buckets": buckets, "total": len(tasks),
                "counts": {k: len(v) for k, v in buckets.items()}}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_my_work")
        return {"buckets": {"overdue": [], "today": [], "upcoming": [], "no_date": []}, "total": 0, "counts": {}}


def _ensure_activity_type():
    name = frappe.db.get_value("Activity Type", {}, "name")
    if name:
        return name
    doc = frappe.get_doc({"doctype": "Activity Type", "activity_type": "Execution"})
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist()
def get_my_timesheets(user=None):
    user = user or frappe.session.user
    try:
        emp = frappe.db.get_value("Employee", {"user_id": user}, "name")
        filt = {"employee": emp} if emp else {"owner": user}
        rows = frappe.get_all("Timesheet", filters=filt,
            fields=["name", "total_hours", "total_billable_hours", "status",
                    "start_date", "end_date", "docstatus"],
            order_by="modified desc", limit=100)
        week_total = sum(flt(r.total_hours) for r in rows[:7])
        return {"timesheets": rows, "has_employee": bool(emp), "recent_hours": week_total}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_my_timesheets")
        return {"timesheets": [], "has_employee": False}


@frappe.whitelist()
def create_timesheet_log(hours, date=None, task=None, project=None, activity_type=None, description=None, company=None):
    try:
        company = company or _get_company()
        user = frappe.session.user
        emp = frappe.db.get_value("Employee", {"user_id": user}, "name")
        act = activity_type or _ensure_activity_type()
        d = date or nowdate()
        if task and not project:
            project = frappe.db.get_value("Task", task, "project")
        ts = frappe.get_doc({
            "doctype": "Timesheet", "company": company, "employee": emp or None,
            "time_logs": [{
                "activity_type": act, "hours": flt(hours),
                "task": task or None, "project": project or None,
                "from_time": f"{d} 09:00:00", "description": description or None,
            }],
        })
        ts.flags.ignore_permissions = True
        ts.insert(ignore_permissions=True)
        frappe.db.commit()
        return {"name": ts.name, "total_hours": ts.total_hours}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: create_timesheet_log")
        frappe.throw(str(e))


@frappe.whitelist()
def get_project_profitability(company=None):
    company = company or _get_company()
    try:
        rows = frappe.get_all("Project",
            filters={"company": company} if company else {},
            fields=["name", "project_name", "status", "customer", "percent_complete",
                    "estimated_costing", "total_costing_amount", "total_purchase_cost",
                    "total_billable_amount", "total_billed_amount", "gross_margin", "per_gross_margin"],
            order_by="modified desc", limit=200)
        tot = {"billable": 0, "billed": 0, "cost": 0, "margin": 0}
        for r in rows:
            cost = flt(r.total_costing_amount) + flt(r.total_purchase_cost)
            r["actual_cost"] = cost
            r["margin"] = flt(r.total_billable_amount) - cost
            r["margin_pct"] = round(r["margin"] / flt(r.total_billable_amount) * 100) if flt(r.total_billable_amount) else 0
            tot["billable"] += flt(r.total_billable_amount)
            tot["billed"] += flt(r.total_billed_amount)
            tot["cost"] += cost
            tot["margin"] += r["margin"]
        tot["margin_pct"] = round(tot["margin"] / tot["billable"] * 100) if tot["billable"] else 0
        return {"projects": rows, "totals": tot}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: get_project_profitability")
        return {"projects": [], "totals": {}}


# ══════════════════════════════════════════════════════════════════
#  HR: Holiday Lists with country presets
# ══════════════════════════════════════════════════════════════════

# Fixed-date national/public holidays per country (month, day, name).
# Islamic holidays (Eid etc.) shift yearly and are left for manual addition.
HOLIDAY_PRESETS = {
    "UAE": [(1,1,"New Year's Day"),(12,1,"Commemoration Day"),(12,2,"National Day"),(12,3,"National Day Holiday")],
    "UK": [(1,1,"New Year's Day"),(12,25,"Christmas Day"),(12,26,"Boxing Day")],
    "Pakistan": [(2,5,"Kashmir Day"),(3,23,"Pakistan Day"),(5,1,"Labour Day"),(8,14,"Independence Day"),(11,9,"Iqbal Day"),(12,25,"Quaid-e-Azam Day")],
    "Philippines": [(1,1,"New Year's Day"),(4,9,"Day of Valor"),(5,1,"Labor Day"),(6,12,"Independence Day"),(8,21,"Ninoy Aquino Day"),(11,1,"All Saints' Day"),(11,30,"Bonifacio Day"),(12,25,"Christmas Day"),(12,30,"Rizal Day")],
    "Türkiye": [(1,1,"New Year's Day"),(4,23,"National Sovereignty & Children's Day"),(5,1,"Labour Day"),(5,19,"Commemoration of Atatürk"),(7,15,"Democracy Day"),(8,30,"Victory Day"),(10,29,"Republic Day")],
}


@frappe.whitelist()
def get_holiday_presets():
    return {"countries": list(HOLIDAY_PRESETS.keys())}


@frappe.whitelist()
def get_holiday_lists():
    try:
        lists = frappe.get_all("Holiday List",
            fields=["name", "holiday_list_name", "from_date", "to_date", "total_holidays"],
            order_by="from_date desc", limit=100)
        return lists
    except Exception:
        return []


@frappe.whitelist()
def create_holiday_list(title, year, country=None, weekly_off=None, extra_holidays=None):
    """Create a Holiday List for a year, pre-filled with a country's public holidays
    and (optionally) a recurring weekly off day."""
    import json as _json
    from frappe.utils import getdate
    try:
        year = int(year)
        from_date, to_date = f"{year}-01-01", f"{year}-12-31"
        doc = frappe.get_doc({
            "doctype": "Holiday List",
            "holiday_list_name": title or f"{country or 'Company'} {year}",
            "from_date": from_date, "to_date": to_date,
        })
        if weekly_off:
            doc.weekly_off = weekly_off

        added = 0
        for (mo, day, name) in HOLIDAY_PRESETS.get(country, []):
            try:
                d = getdate(f"{year}-{mo:02d}-{day:02d}")
                doc.append("holidays", {"holiday_date": str(d), "description": name})
                added += 1
            except Exception:
                pass
        for h in (_json.loads(extra_holidays) if isinstance(extra_holidays, str) else (extra_holidays or [])):
            if h.get("date"):
                doc.append("holidays", {"holiday_date": h["date"], "description": h.get("name") or "Holiday"})
                added += 1

        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        # Auto-populate weekly offs (Saturdays/Sundays etc.) if requested.
        if weekly_off:
            try:
                doc.get_weekly_off_dates()
                doc.save(ignore_permissions=True)
            except Exception:
                pass
        frappe.db.commit()
        return {"name": doc.name, "holidays_added": added, "total": doc.total_holidays}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: create_holiday_list")
        frappe.throw(str(e))


@frappe.whitelist()
def assign_holiday_list(holiday_list, employees=None, all_active=0):
    """Set a holiday list on selected employees (or all active)."""
    import json as _json
    try:
        if int(all_active or 0):
            emp_list = frappe.get_all("Employee", filters={"status": "Active"}, pluck="name")
        else:
            emp_list = _json.loads(employees) if isinstance(employees, str) else (employees or [])
        n = 0
        for e in emp_list:
            frappe.db.set_value("Employee", e, "holiday_list", holiday_list)
            n += 1
        frappe.db.commit()
        return {"updated": n}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Portal: assign_holiday_list")
        frappe.throw(str(e))


# ══════════════════════════════════════════════════════════════════
#  SERVICE PROPOSAL TEMPLATES (scope of work + terms + costs per service)
# ══════════════════════════════════════════════════════════════════

SERVICE_TEMPLATES = [
    {
        "key": "meydan_formation", "name": "Meydan Free Zone — Company Formation + Visa",
        "category": "Company Formation", "icon": "fa-building-flag",
        "scope": [
            "Reservation of trade name and initial approval with Meydan Free Zone.",
            "Issuance of Free Zone Commercial/Service License (1 visa allocation).",
            "Preparation of MOA / incorporation documents and lease (Ejari/flexi-desk).",
            "Establishment Card and E-Channel immigration registration.",
            "Investor/Employment visa processing: entry permit, status change, medical, Emirates ID and visa stamping.",
            "Corporate bank account introduction and assistance.",
        ],
        "terms": [
            "Government, Free Zone and immigration fees are at actuals and may change without notice.",
            "Timelines are subject to authority approvals and applicant document readiness.",
            "Professional fees are non-refundable once the application is lodged with the authority.",
            "Medical fitness and security clearance are prerequisites for visa issuance.",
            "Quotation valid for 30 days from the date of issue.",
        ],
        "items": [
            {"description": "Meydan FZ Commercial License (1 Visa allocation)", "qty": 1, "rate": 14900},
            {"description": "Establishment Card + E-Channel Registration", "qty": 1, "rate": 3500},
            {"description": "Investor Visa (2 years) — entry permit, medical, EID, stamping", "qty": 1, "rate": 4000},
            {"description": "PRO & Documentation Service Fee", "qty": 1, "rate": 1500},
        ],
    },
    {
        "key": "regulated_activity", "name": "Regulated Activity Licensing & Approvals",
        "category": "Company Formation", "icon": "fa-shield-halved",
        "scope": [
            "Assessment of the regulated activity and the competent authority (e.g. Central Bank, SCA, DET, KHDA, DHA, etc.).",
            "Preparation and submission of the special/external approval application.",
            "Coordination with the regulator for inspections, qualifications and compliance evidence.",
            "Issuance of the activity-specific license with the regulated approval annotated.",
        ],
        "terms": [
            "Regulatory approval is at the sole discretion of the competent authority.",
            "Additional capital, qualifications, insurance or office requirements may be imposed by the regulator.",
            "External approval fees are at actuals and vary by authority and activity.",
            "Quotation valid for 30 days; timelines depend on regulator response.",
        ],
        "items": [
            {"description": "Regulated Activity Approval — application & coordination", "qty": 1, "rate": 6500},
            {"description": "External / Special Approval Authority Fee (at actuals — estimate)", "qty": 1, "rate": 5000},
        ],
    },
    {
        "key": "accounting", "name": "Accounting & Financial Statements (Annual)",
        "category": "Accounting", "icon": "fa-calculator",
        "scope": [
            "Maintenance of the general ledger and chart of accounts.",
            "Preparation of monthly management accounts and reconciliations.",
            "Preparation of annual financial statements (Balance Sheet, P&L, Cash Flow).",
            "Coordination with auditors and provision of supporting schedules.",
        ],
        "terms": [
            "Fees are based on the agreed transaction volume; material changes may be re-quoted.",
            "Client to provide source documents in a timely manner each month.",
            "Engagement excludes statutory audit unless separately agreed.",
            "Quotation valid for 30 days from issue.",
        ],
        "items": [
            {"description": "Accounting & Financial Statements (Annual)", "qty": 1, "rate": 12000},
        ],
    },
    {
        "key": "bookkeeping", "name": "Bookkeeping (Monthly)",
        "category": "Accounting", "icon": "fa-book",
        "scope": [
            "Recording of sales, purchases, expenses and bank transactions.",
            "Monthly bank and supplier/customer reconciliations.",
            "Maintenance of accounts on cloud accounting software.",
            "Monthly trial balance and basic management reports.",
        ],
        "terms": [
            "Monthly retainer billed in advance; minimum 12-month engagement.",
            "Transaction volume thresholds apply; excess volume re-quoted.",
            "Quotation valid for 30 days from issue.",
        ],
        "items": [
            {"description": "Bookkeeping Service (Monthly retainer)", "qty": 12, "rate": 600},
        ],
    },
    {
        "key": "tax_residency", "name": "Tax Residency Certificate (TRC)",
        "category": "Tax", "icon": "fa-passport",
        "scope": [
            "Eligibility assessment for UAE Tax Residency (individual or corporate).",
            "Compilation of supporting documents (tenancy, bank statements, immigration report).",
            "Application submission on the Federal Tax Authority (FTA) portal.",
            "Follow-up with the FTA until issuance of the TRC.",
        ],
        "terms": [
            "Issuance is subject to FTA approval and meeting the residency criteria (183/90 days as applicable).",
            "FTA government fees are at actuals and payable in advance.",
            "Professional fee is non-refundable once the application is submitted.",
            "Quotation valid for 30 days from issue.",
        ],
        "items": [
            {"description": "Tax Residency Certificate (TRC) — application & follow-up", "qty": 1, "rate": 4500},
            {"description": "FTA Government Fee (at actuals — estimate)", "qty": 1, "rate": 2000},
        ],
    },
    {
        "key": "vat_registration", "name": "VAT Registration",
        "category": "Tax", "icon": "fa-receipt",
        "scope": [
            "Assessment of mandatory/voluntary VAT registration threshold.",
            "Preparation of the VAT registration application and supporting documents.",
            "Submission on the FTA EmaraTax portal and liaison until TRN issuance.",
            "Guidance on VAT invoicing and record-keeping obligations.",
        ],
        "terms": [
            "Registration outcome and TRN issuance are subject to FTA approval.",
            "Penalties for late registration (if any) are the client's responsibility.",
            "Quotation valid for 30 days from issue.",
        ],
        "items": [
            {"description": "VAT Registration — application & TRN issuance", "qty": 1, "rate": 1000},
        ],
    },
    {
        "key": "corporate_tax", "name": "Corporate Tax Registration & Filing",
        "category": "Tax", "icon": "fa-landmark",
        "scope": [
            "Corporate Tax registration on the FTA EmaraTax portal.",
            "Assessment of taxable income, reliefs and Small Business Relief eligibility.",
            "Preparation and filing of the annual Corporate Tax return.",
            "Maintenance of supporting computations and documentation.",
        ],
        "terms": [
            "Based on a simple return; transfer pricing / complex adjustments quoted separately.",
            "Client to provide audited/finalised financials before the filing deadline.",
            "FTA penalties for late registration or filing are the client's responsibility.",
            "Quotation valid for 30 days from issue.",
        ],
        "items": [
            {"description": "Corporate Tax Registration", "qty": 1, "rate": 750},
            {"description": "Corporate Tax Filing — Simple Return (Annual)", "qty": 1, "rate": 2000},
        ],
    },
    {
        "key": "vat_return", "name": "VAT Return Filing (Quarterly)",
        "category": "Tax", "icon": "fa-file-invoice",
        "scope": [
            "Review of sales and purchase records for the tax period.",
            "Computation of output and recoverable input VAT.",
            "Preparation and filing of the VAT return (Form 201) on EmaraTax.",
            "Advisory on payment and record-keeping.",
        ],
        "terms": [
            "Billed per return; assumes books are maintained and reconciled.",
            "Client responsible for settling any VAT payable to the FTA by the due date.",
            "Quotation valid for 30 days from issue.",
        ],
        "items": [
            {"description": "VAT Return Filing (per quarter)", "qty": 4, "rate": 1000},
        ],
    },
]


@frappe.whitelist()
def get_service_templates():
    """Catalog of service proposals — scope of work, terms and indicative costs."""
    return SERVICE_TEMPLATES
