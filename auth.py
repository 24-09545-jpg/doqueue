# auth.py
"""
Session handling, role-based access control and the per-portal shell.

Three portals share the existing dashboard layout but have separate logins:
  User Portal      (/login)            role = 'user'       (organizations)
  Admin Portal     (/admin/login)      role = 'office'     (signatory offices)
  Moderator Portal (/moderator/login)  role = 'moderator'  (DSA management)
"""
import secrets
from functools import wraps

from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for

from models import find_user_by_id

PORTALS = {
    "user": {"name": "User Portal", "login": "login", "home": "dashboard"},
    "office": {"name": "Admin Portal", "login": "admin.login", "home": "admin.dashboard"},
    "moderator": {"name": "Moderator Portal", "login": "moderator.login", "home": "moderator.dashboard"},
}


def _icon(name):
    return f"img/icon/{name}.png"


# (page key, label, icon, endpoint)
NAV = {
    "user": [
        ("dashboard", "Dashboard", _icon("dash"), "dashboard_partial"),
        ("sign_yourself", "Sign Yourself", _icon("book"), "sign_yourself_partial"),
        ("request_signature", "Request Signature", _icon("sig"), "upload_document_partial"),
        ("notifications", "Notifications", _icon("need"), "notifications_partial"),
        ("settings", "Settings", _icon("settings"), "settings_partial"),
        ("contacts", "Contacts", _icon("contact"), "contacts_partial"),
    ],
    "office": [
        ("dashboard", "Queue", _icon("dash"), "admin.queue_partial"),
        ("history", "Signing History", _icon("tick"), "admin.history_partial"),
        ("sign_yourself", "Sign Yourself", _icon("book"), "sign_yourself_partial"),
        ("notifications", "Notifications", _icon("need"), "notifications_partial"),
        ("settings", "Settings", _icon("settings"), "settings_partial"),
        ("contacts", "Contacts", _icon("contact"), "contacts_partial"),
    ],
    "moderator": [
        ("dashboard", "Overview", _icon("dash"), "moderator.overview_partial"),
        ("letters", "Letters", _icon("book"), "moderator.letters_partial"),
        ("offices", "Offices", _icon("building"), "moderator.offices_partial"),
        ("accounts", "Accounts", _icon("contact"), "moderator.accounts_partial"),
        ("hierarchy", "Signing Hierarchy", _icon("sig"), "moderator.hierarchy_partial"),
        ("content", "Website Content", _icon("email"), "moderator.content_partial"),
        ("activity", "Activity Log", _icon("clock"), "moderator.activity_partial"),
        ("notifications", "Notifications", _icon("need"), "notifications_partial"),
    ],
}


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = find_user_by_id(user_id)
    if not user or not user["is_active"]:
        session.clear()
        return None
    return user


def home_url_for(user):
    if not user:
        return url_for("login")
    return url_for(PORTALS[user["role"]]["home"])


def login_url_for_role(role):
    return url_for(PORTALS.get(role, PORTALS["user"])["login"])


def _is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def roles_required(*roles):
    """Restrict a view to the given roles. Wrong-role users are sent to their own portal."""

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                if _is_ajax():
                    return jsonify({"ok": False, "error": "Session expired. Please sign in again."}), 401
                return redirect(login_url_for_role(roles[0] if roles else "user"))
            if roles and user["role"] not in roles:
                if _is_ajax() or request.method != "GET":
                    abort(403)
                flash("You do not have access to that portal.", "error")
                return redirect(home_url_for(user))
            return f(*args, **kwargs)

        return wrapper

    return decorator


login_required = roles_required()


# ───────────── CSRF ─────────────

def csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
    if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
        if _is_ajax():
            return jsonify({"ok": False, "error": "Your session expired. Please reload the page."}), 400
        flash("Your session expired. Please try again.", "error")
        return redirect(request.referrer or url_for("index"))
    return None


# ───────────── portal shell ─────────────

def nav_for(user):
    return [
        {"page": p, "label": label, "icon": icon, "url": url_for(ep)}
        for (p, label, icon, ep) in NAV[user["role"]]
    ]


def render_shell(user, active_page, partial_template, **ctx):
    """Render the shared dashboard layout with the given partial pre-loaded."""
    return render_template(
        "dashboard.html",
        user=user,
        nav_items=nav_for(user),
        active_page=active_page,
        initial_partial=partial_template,
        portal_name=PORTALS[user["role"]]["name"],
        **ctx,
    )
