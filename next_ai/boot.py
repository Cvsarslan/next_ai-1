import frappe


def boot_session(bootinfo):
    """Inject WhatsApp chat button config into frappe.boot so the frontend
    can read it without an extra API call."""
    try:
        bootinfo.wa_chat_enabled     = frappe.db.get_single_value("System Settings", "wa_chat_enabled") or 0
        bootinfo.wa_chat_phone       = frappe.db.get_single_value("System Settings", "wa_chat_phone") or ""
        bootinfo.wa_chat_agent_name  = frappe.db.get_single_value("System Settings", "wa_chat_agent_name") or "Support"
        bootinfo.wa_chat_agent_status = frappe.db.get_single_value("System Settings", "wa_chat_agent_status") or "Typically replies within minutes"
        bootinfo.wa_chat_greeting    = frappe.db.get_single_value("System Settings", "wa_chat_greeting") or "Hello! I need assistance."
    except Exception:
        pass
