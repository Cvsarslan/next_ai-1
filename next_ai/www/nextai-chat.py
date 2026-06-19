import frappe


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/nextai-chat"
        raise frappe.Redirect

    user = frappe.session.user
    roles = frappe.get_roles(user)

    if not any(r in roles for r in ["System Manager", "NextAI User", "Administrator"]):
        frappe.throw(
            "You do not have permission to access NextAI Chat.",
            frappe.PermissionError,
        )

    context.no_cache = 1
    context.title = "NextAI Chat"
    context.user_fullname = frappe.get_value("User", user, "full_name") or user
