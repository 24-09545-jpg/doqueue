# routes_moderator.py
"""Moderator Portal – DSA system management (not part of the signing process)."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

import workflow
from auth import current_user, render_shell, roles_required
from db import execute, now_str, query, transaction
from models import (DEFAULT_CONTENT_KEYS, IN_FLIGHT_LETTER_STATUSES, LETTER_STATUS_LABELS,
                    all_content, find_office_by_id, find_org_by_name, find_user_by_email,
                    find_user_by_id, get_hierarchy, hash_password, list_offices)

bp = Blueprint("moderator", __name__, url_prefix="/moderator")


@bp.route("/login", methods=["GET", "POST"])
def login():
    from app import portal_login
    return portal_login("moderator")


def _back(page):
    return redirect(url_for("moderator.dashboard") + "#" + page)


def _log(action, details, **kw):
    workflow.log_activity(current_user(), action, details, **kw)


# ───────────── Overview / monitoring ─────────────

def _overview_ctx():
    offices = workflow.office_directory()
    for o in offices:
        o["counts"] = workflow.office_counts(o["id"])
    return {
        "stats": workflow.system_stats(),
        "offices": offices,
        "recent": query("SELECT * FROM activity_log ORDER BY id DESC LIMIT 15"),
        "pending_accounts": query(
            """SELECT u.*, g.name AS organization_name FROM users u
                 LEFT JOIN organizations g ON g.id = u.organization_id
                WHERE u.is_active = 0 AND u.last_login_at IS NULL AND u.role = 'user'
                ORDER BY u.id DESC"""),
        "hierarchy": get_hierarchy(),
    }


@bp.route("/")
@roles_required("moderator")
def dashboard():
    return render_shell(current_user(), "dashboard", "partials/mod_overview.html", **_overview_ctx())


@bp.route("/p/overview")
@roles_required("moderator")
def overview_partial():
    return render_template("partials/mod_overview.html", **_overview_ctx())


@bp.route("/p/letters")
@roles_required("moderator")
def letters_partial():
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    sql = """SELECT l.*, g.name AS organization_name, u.name AS submitter_name,
                    (SELECT s.office_name FROM letter_steps s WHERE s.letter_id = l.id AND s.cycle = l.current_cycle
                       AND s.status IN ('queued','processing','awaiting_signature') ORDER BY s.step_order LIMIT 1) AS current_office
               FROM letters l JOIN organizations g ON g.id = l.organization_id
               JOIN users u ON u.id = l.submitted_by WHERE 1 = 1"""
    params = []
    if status == "in_progress":
        sql += f" AND l.status IN ({','.join('?' * len(IN_FLIGHT_LETTER_STATUSES))})"
        params += list(IN_FLIGHT_LETTER_STATUSES)
    elif status:
        sql += " AND l.status = ?"
        params.append(status)
    if q:
        sql += " AND (l.title LIKE ? OR g.name LIKE ? OR l.tracking_number LIKE ?)"
        params += [f"%{q}%"] * 3
    sql += " ORDER BY l.updated_at DESC LIMIT 500"
    letters = query(sql, params)
    for l in letters:
        l["progress"] = workflow.letter_progress(l)
    return render_template("partials/mod_letters.html", letters=letters, status=status, q=q,
                           LETTER_STATUS_LABELS=LETTER_STATUS_LABELS)


# ───────────── Offices ─────────────

@bp.route("/p/offices")
@roles_required("moderator")
def offices_partial():
    offices = list_offices()
    accounts = query("SELECT id, name, email, office_id, is_active, position FROM users "
                     "WHERE role = 'office' ORDER BY name COLLATE NOCASE")
    return render_template("partials/mod_offices.html", offices=offices, accounts=accounts)


@bp.route("/offices/save", methods=["POST"])
@roles_required("moderator")
def office_save():
    f = request.form
    office_id = f.get("id", type=int)
    name = f.get("name", "").strip()
    if not name:
        flash("Office name is required.", "error")
        return _back("offices")
    fields = (name, f.get("description", "").strip(), f.get("contact_email", "").strip(),
              f.get("contact_phone", "").strip(), f.get("location", "").strip())
    dup = query("SELECT id FROM offices WHERE name = ? COLLATE NOCASE AND id != ?", (name, office_id or 0), one=True)
    if dup:
        flash("Another office already uses that name.", "error")
        return _back("offices")

    signatory_id = f.get("signatory_user_id", type=int)
    old = find_office_by_id(office_id) if office_id else None
    if office_id and not old:
        abort(404)
    if office_id and signatory_id:
        acc = query("SELECT * FROM users WHERE id = ? AND role = 'office' AND office_id = ? AND is_active = 1",
                    (signatory_id, office_id), one=True)
        if not acc:
            flash("The designated signatory must be an active account of this office.", "error")
            return _back("offices")
    with transaction():
        if office_id:
            execute("""UPDATE offices SET name = ?, description = ?, contact_email = ?, contact_phone = ?,
                              location = ?, signatory_user_id = ?, updated_at = ? WHERE id = ?""",
                    (*fields, signatory_id, now_str(), office_id))
            changes = []
            if old["name"] != name:
                changes.append(f"renamed '{old['name']}' → '{name}'")
            if old["signatory_user_id"] != signatory_id:
                changes.append("designated signatory changed")
            _log("office_updated", f"{name}: " + (", ".join(changes) or "details updated"), office_id=office_id)
            if old["signatory_user_id"] != signatory_id and signatory_id:
                workflow.notify([signatory_id], f"You were designated as the signatory of {name}.", "account")
            flash("Office updated. Letters already in progress keep their routing.", "success")
        else:
            new_id = execute("""INSERT INTO offices(name, description, contact_email, contact_phone, location,
                                                     created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                             (*fields, now_str(), now_str()))
            _log("office_created", f"Created office '{name}'", office_id=new_id)
            flash("Office added. Add it to the Signing Hierarchy to include it in the signing process.", "success")
    return _back("offices")


@bp.route("/offices/<int:office_id>/toggle", methods=["POST"])
@roles_required("moderator")
def office_toggle(office_id):
    office = find_office_by_id(office_id)
    if not office:
        abort(404)
    with transaction():
        if office["is_active"]:
            busy = workflow.office_counts(office_id)
            if busy["waiting"] or busy["processing"]:
                flash(f"Cannot deactivate {office['name']}: it still has {busy['waiting'] + busy['processing']} "
                      f"letter(s) in its active queue.", "error")
                return _back("offices")
            execute("UPDATE offices SET is_active = 0, updated_at = ? WHERE id = ?", (now_str(), office_id))
            _log("office_deactivated", office["name"], office_id=office_id)
            flash(f"{office['name']} deactivated. It will be skipped for new submissions.", "success")
        else:
            execute("UPDATE offices SET is_active = 1, updated_at = ? WHERE id = ?", (now_str(), office_id))
            _log("office_reactivated", office["name"], office_id=office_id)
            flash(f"{office['name']} reactivated.", "success")
    return _back("offices")


# ───────────── Accounts & organizations ─────────────

@bp.route("/p/accounts")
@roles_required("moderator")
def accounts_partial():
    users = query("""SELECT u.*, o.name AS office_name, g.name AS organization_name
                       FROM users u LEFT JOIN offices o ON o.id = u.office_id
                       LEFT JOIN organizations g ON g.id = u.organization_id
                      ORDER BY u.role, u.name COLLATE NOCASE""")
    orgs = query("""SELECT g.*, (SELECT COUNT(*) FROM users u WHERE u.organization_id = g.id) AS members,
                           (SELECT COUNT(*) FROM letters l WHERE l.organization_id = g.id) AS letters
                      FROM organizations g ORDER BY g.name COLLATE NOCASE""")
    return render_template("partials/mod_accounts.html", users=users, orgs=orgs,
                           offices=list_offices(), me=current_user())


@bp.route("/accounts/save", methods=["POST"])
@roles_required("moderator")
def account_save():
    f = request.form
    uid = f.get("id", type=int)
    name = f.get("name", "").strip()
    email = f.get("email", "").strip().lower()
    role = f.get("role", "")
    password = f.get("password", "")
    position = f.get("position", "").strip()

    if not name or not email or role not in ("user", "office", "moderator"):
        flash("Name, email and role are required.", "error")
        return _back("accounts")
    other = find_user_by_email(email)
    if other and other["id"] != uid:
        flash("That email is already used by another account.", "error")
        return _back("accounts")

    office_id = org_id = None
    if role == "office":
        office_id = f.get("office_id", type=int)
        if not office_id or not find_office_by_id(office_id):
            flash("Please choose the office for this signatory account.", "error")
            return _back("accounts")
    elif role == "user":
        org_name = f.get("organization", "").strip()
        if not org_name:
            flash("Please enter the organization for this account.", "error")
            return _back("accounts")
        org = find_org_by_name(org_name)
        org_id = org["id"] if org else None

    if (password or not uid) and len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return _back("accounts")
    old = find_user_by_id(uid) if uid else None
    if uid and not old:
        abort(404)
    if old and old["role"] == "moderator" and role != "moderator" and _active_moderators() <= 1:
        flash("You cannot change the role of the last active moderator.", "error")
        return _back("accounts")

    with transaction():
        if role == "user" and org_id is None:
            org_id = execute("INSERT INTO organizations(name, contact_email, created_at) VALUES (?, ?, ?)",
                             (f.get("organization", "").strip(), email, now_str()))
        if uid:
            execute("""UPDATE users SET name = ?, email = ?, role = ?, office_id = ?, organization_id = ?,
                              position = ? WHERE id = ?""",
                    (name, email, role, office_id, org_id, position, uid))
            if password:
                execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), uid))
            if old["office_id"] and old["office_id"] != office_id:
                # moved out of an office: it can no longer be that office's designated signatory
                execute("UPDATE offices SET signatory_user_id = NULL WHERE id = ? AND signatory_user_id = ?",
                        (old["office_id"], uid))
            _log("account_updated", f"{email} ({role})" + (" – password reset" if password else ""))
            flash("Account updated.", "success")
        else:
            new_id = execute(
                """INSERT INTO users(email, password_hash, name, role, organization_id, office_id, position,
                                     is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (email, hash_password(password), name, role, org_id, office_id, position, now_str()))
            if role == "office" and f.get("make_signatory"):
                execute("UPDATE offices SET signatory_user_id = ?, updated_at = ? WHERE id = ?",
                        (new_id, now_str(), office_id))
            _log("account_created", f"{email} ({role})", office_id=office_id)
            flash("Account created.", "success")
    return _back("accounts")


def _active_moderators():
    return query("SELECT COUNT(*) AS n FROM users WHERE role = 'moderator' AND is_active = 1", one=True)["n"]


@bp.route("/accounts/<int:user_id>/toggle", methods=["POST"])
@roles_required("moderator")
def account_toggle(user_id):
    target = find_user_by_id(user_id)
    me = current_user()
    if not target:
        abort(404)
    if target["id"] == me["id"]:
        flash("You cannot deactivate your own account.", "error")
        return _back("accounts")
    with transaction():
        if target["is_active"]:
            if target["role"] == "moderator" and _active_moderators() <= 1:
                flash("You cannot deactivate the last active moderator.", "error")
                return _back("accounts")
            execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
            _log("account_deactivated", f"{target['email']} ({target['role']})")
            msg = f"{target['email']} deactivated."
            if target["role"] == "office" and target["office_id"]:
                left = query("SELECT COUNT(*) AS n FROM users WHERE office_id = ? AND role = 'office' AND is_active = 1",
                             (target["office_id"],), one=True)["n"]
                if left == 0:
                    msg += f" Warning: {target['office_name']} now has no active account and is shown as Unavailable."
            flash(msg, "success")
        else:
            execute("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))
            _log("account_activated", f"{target['email']} ({target['role']})")
            workflow.notify([user_id], "Your DSA System account has been activated.", "account")
            flash(f"{target['email']} activated.", "success")
    return _back("accounts")


@bp.route("/organizations/<int:org_id>/save", methods=["POST"])
@roles_required("moderator")
def organization_save(org_id):
    org = query("SELECT * FROM organizations WHERE id = ?", (org_id,), one=True)
    if not org:
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Organization name is required.", "error")
        return _back("accounts")
    dup = query("SELECT id FROM organizations WHERE name = ? COLLATE NOCASE AND id != ?", (name, org_id), one=True)
    if dup:
        flash("Another organization already uses that name.", "error")
        return _back("accounts")
    with transaction():
        execute("UPDATE organizations SET name = ?, description = ?, contact_email = ? WHERE id = ?",
                (name, request.form.get("description", "").strip(),
                 request.form.get("contact_email", "").strip(), org_id))
        _log("organization_updated", name)
    flash("Organization updated.", "success")
    return _back("accounts")


@bp.route("/organizations/<int:org_id>/toggle", methods=["POST"])
@roles_required("moderator")
def organization_toggle(org_id):
    org = query("SELECT * FROM organizations WHERE id = ?", (org_id,), one=True)
    if not org:
        abort(404)
    new = 0 if org["is_active"] else 1
    with transaction():
        execute("UPDATE organizations SET is_active = ? WHERE id = ?", (new, org_id))
        if not new:
            execute("UPDATE users SET is_active = 0 WHERE organization_id = ?", (org_id,))
        _log("organization_" + ("reactivated" if new else "deactivated"), org["name"])
    flash(f"{org['name']} {'reactivated (re-activate member accounts individually)' if new else 'deactivated with its member accounts'}.",
          "success")
    return _back("accounts")


# ───────────── Signing hierarchy ─────────────

@bp.route("/p/hierarchy")
@roles_required("moderator")
def hierarchy_partial():
    hierarchy = get_hierarchy()
    in_h = {h["id"] for h in hierarchy}
    directory = {o["id"]: o for o in workflow.office_directory()}
    for h in hierarchy:
        d = directory.get(h["id"])
        h["active_accounts"] = d["active_accounts"] if d else 0
    return render_template("partials/mod_hierarchy.html", hierarchy=hierarchy,
                           available=[o for o in list_offices() if o["id"] not in in_h])


@bp.route("/hierarchy/save", methods=["POST"])
@roles_required("moderator")
def hierarchy_save():
    ids = []
    for raw in request.form.getlist("office_ids"):
        try:
            oid = int(raw)
        except ValueError:
            continue
        if oid not in ids:
            ids.append(oid)
    valid = {o["id"]: o for o in list_offices()}
    ids = [i for i in ids if i in valid]
    if not ids:
        flash("The signing hierarchy needs at least one office.", "error")
        return _back("hierarchy")
    before = " → ".join(h["name"] for h in get_hierarchy()) or "(empty)"
    with transaction():
        execute("DELETE FROM hierarchy_steps")
        for order, oid in enumerate(ids, start=1):
            execute("INSERT INTO hierarchy_steps(step_order, office_id) VALUES (?, ?)", (order, oid))
        after = " → ".join(valid[i]["name"] for i in ids)
        _log("hierarchy_updated", f"{before}  ⇒  {after}")
    flash("Signing hierarchy saved. It applies to new submissions and resubmissions; "
          "letters already in progress keep their current route.", "success")
    return _back("hierarchy")


# ───────────── Website content ─────────────

@bp.route("/p/content")
@roles_required("moderator")
def content_partial():
    return render_template("partials/mod_content.html", content=all_content())


@bp.route("/content/save", methods=["POST"])
@roles_required("moderator")
def content_save():
    changed = []
    with transaction():
        current = all_content()
        for key in DEFAULT_CONTENT_KEYS:
            if key in request.form:
                val = request.form.get(key, "").strip()
                if current.get(key, "") != val:
                    execute("""INSERT INTO site_content(key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)
                               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                 updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
                            (key, val, now_str(), current_user()["id"]))
                    changed.append(key)
        if changed:
            _log("content_updated", "Updated: " + ", ".join(changed))
    flash("Website content saved." if changed else "No changes to save.", "success")
    return _back("content")


# ───────────── Activity log ─────────────

@bp.route("/p/activity")
@roles_required("moderator")
def activity_partial():
    role = request.args.get("role", "")
    q = request.args.get("q", "").strip()
    sql = """SELECT a.*, o.name AS office_name, l.title AS letter_title FROM activity_log a
               LEFT JOIN offices o ON o.id = a.office_id
               LEFT JOIN letters l ON l.id = a.letter_id WHERE 1 = 1"""
    params = []
    if role:
        sql += " AND a.actor_role = ?"
        params.append(role)
    if q:
        sql += " AND (a.action LIKE ? OR a.details LIKE ? OR a.actor_name LIKE ? OR l.title LIKE ?)"
        params += [f"%{q}%"] * 4
    sql += " ORDER BY a.id DESC LIMIT 500"
    return render_template("partials/mod_activity.html", entries=query(sql, params), role=role, q=q)
