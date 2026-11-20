# app.py
import os
import re
import time
import uuid
from datetime import date, datetime
from os.path import splitext

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

import db
import pdfsign
import workflow
from auth import (PORTALS, check_csrf, csrf_token, current_user, home_url_for, login_required,
                  login_url_for_role, render_shell, roles_required)
from db import execute, now_str, query, transaction
from models import (
    LETTER_STATUS_LABELS, REVISABLE_LETTER_STATUSES, STEP_STATUS_LABELS,
    all_content, create_org, create_user, find_letter_by_id, find_org_by_name, find_user_by_email,
    find_user_by_id, get_comments, get_content, get_hierarchy, get_letter_activity, get_steps,
    get_versions, moderator_ids, parse_contact_rows, verify_password,
)
from workflow import WorkflowError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.secret_key = os.environ.get("DSA_SECRET_KEY", "secret-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB uploads
UPLOAD_FOLDER = os.environ.get("DSA_UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
workflow.configure(UPLOAD_FOLDER)

ALLOWED_DOMAIN = "@g.batstate-u.edu.ph"

app.teardown_appcontext(db.close_db)
with app.app_context():
    db.init_db()


# ───────────── request hooks / template helpers ─────────────

_last_expiry_check = [0.0]


@app.before_request
def _before():
    resp = check_csrf()
    if resp is not None:
        return resp
    csrf_token()  # make sure every session has a token
    if time.time() - _last_expiry_check[0] > 60:
        _last_expiry_check[0] = time.time()
        try:
            workflow.expire_overdue_letters()
        except Exception as e:  # never block a request because of housekeeping
            app.logger.warning("expiry check failed: %s", e)


@app.context_processor
def inject_globals():
    user = current_user()
    return {
        "user": user,
        "csrf_token": csrf_token,
        "unread_notifications": workflow.unread_count(user["id"]) if user else 0,
        "status_label": workflow.status_label,
        "STEP_STATUS_LABELS": STEP_STATUS_LABELS,
        "site": all_content(),
    }


@app.template_filter("dt")
def format_dt(value, with_time=True):
    if not value:
        return "—"
    try:
        d = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            d = datetime.strptime(str(value)[:10], "%Y-%m-%d")
            with_time = False
        except ValueError:
            return value
    return d.strftime("%b %d, %Y %I:%M %p") if with_time else d.strftime("%b %d, %Y")


@app.errorhandler(403)
def _forbidden(_e):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": False, "error": "You are not allowed to do that."}), 403
    return "Forbidden", 403


@app.errorhandler(413)
def _too_large(_e):
    flash("The uploaded file is too large (max 25 MB).", "error")
    return redirect(request.referrer or url_for("index"))


def is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _user_file_name(user, path):
    return os.path.basename(path) if path else None


# ───────────── Uploaded files (locked down) ─────────────

@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    """
    Only serves files that belong to the signed-in user (their signature image and
    their own self-sign files). Letter PDFs are NEVER served from here; they go
    through /letters/<id>/file which enforces the signing sequence.
    """
    user = current_user()
    name = os.path.basename(filename)
    if name != filename:
        abort(404)
    allowed = False
    if user["signature_filename"] and name == user["signature_filename"]:
        allowed = True
    elif name.startswith(f"self_raw_{user['id']}_") or name.startswith(f"signed_self_raw_{user['id']}_"):
        allowed = True
    path = os.path.join(UPLOAD_FOLDER, name)
    if not allowed or not os.path.isfile(path):
        abort(404)
    resp = send_file(path)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _create_signed_pdf(original_pdf_path, signature_image_path, page_index=0, rel_coord=None,
                       out_name=None):
    """Self-sign helper: place one signature using the shared renderer."""
    page_index, rel_x, rel_y, rel_w = rel_coord
    placement = pdfsign.normalize_placement(original_pdf_path, signature_image_path,
                                            page_index, rel_x, rel_y, rel_w)
    placement["image_path"] = signature_image_path
    out = os.path.join(UPLOAD_FOLDER, out_name or f"signed_{os.path.basename(original_pdf_path)}")
    try:
        return pdfsign.render_pdf(original_pdf_path, [placement], out)
    except Exception:
        return None


# ───────────── Auth / portals ─────────────

@app.route("/")
def index():
    user = current_user()
    if user:
        return redirect(home_url_for(user))
    return redirect(url_for("login"))


def portal_login(role):
    """Shared login handler; each portal only accepts accounts of its own role."""
    user = current_user()
    if user and user["role"] == role:
        return redirect(home_url_for(user))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        account = find_user_by_email(email)
        if not account or not verify_password(account, password):
            flash("Invalid email or password.", "error")
        elif account["role"] != role:
            flash(f"This account cannot sign in to the {PORTALS[role]['name']}. "
                  f"Please use the {PORTALS[account['role']]['name']}.", "error")
        elif not account["is_active"]:
            flash("This account is not active yet or has been deactivated. Please contact the DSA.", "error")
        else:
            session.clear()
            session["user_id"] = account["id"]
            session["role"] = account["role"]
            with transaction():
                execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now_str(), account["id"]))
                workflow.log_activity(find_user_by_id(account["id"]), "login",
                                      f"Signed in to {PORTALS[role]['name']}")
            return redirect(url_for(PORTALS[role]["home"]))

    return render_template("login.html", portal_role=role, portal_name=PORTALS[role]["name"],
                           login_action=url_for(PORTALS[role]["login"]))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        org_name = request.form.get("organization", "").strip()
        password = request.form.get("password", "")

        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "error")
            return redirect(url_for("register"))
        if not email.endswith(ALLOWED_DOMAIN):
            flash(f"Registration requires a school email ({ALLOWED_DOMAIN}).", "error")
            return redirect(url_for("register"))
        if not name or not org_name:
            flash("Name and organization are required.", "error")
            return redirect(url_for("register"))
        if find_user_by_email(email):
            flash("Email already used.", "error")
            return redirect(url_for("register"))

        org = find_org_by_name(org_name)
        if org and not org["is_active"]:
            flash("This organization is currently deactivated. Please contact the DSA.", "error")
            return redirect(url_for("register"))
        # Joining an EXISTING organization needs moderator approval so nobody can read
        # another organization's letters just by typing its name.
        needs_approval = org is not None
        org_id = org["id"] if org else create_org(org_name, contact_email=email)
        user_id = create_user(email, password, name, "user", organization_id=org_id,
                              is_active=0 if needs_approval else 1)
        with transaction():
            new_user = find_user_by_id(user_id)
            workflow.log_activity(new_user, "account_registered",
                                  f"Registered for organization '{org_name}'"
                                  + (" (pending approval)" if needs_approval else ""))
            if needs_approval:
                workflow.notify(moderator_ids(),
                                f"{name} ({email}) requested to join '{org_name}'. Approve in Accounts.",
                                "account")
        if needs_approval:
            flash("Account created. Because this organization already exists, a DSA moderator "
                  "must approve your account before you can sign in.", "success")
            return redirect(url_for("login"))

        session.clear()
        session["user_id"] = user_id
        session["role"] = "user"
        flash("Registration successful! Welcome to your dashboard.", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    return portal_login("user")


@app.route("/logout")
def logout():
    role = session.get("role", "user")
    user = current_user()
    if user:
        with transaction():
            workflow.log_activity(user, "logout", "Signed out")
    session.clear()
    return redirect(login_url_for_role(role))


# ───────────── User portal: dashboard ─────────────

def build_dashboard_context(user):
    letters = workflow.letters_for_org(user["organization_id"])
    return {
        "letters": letters,
        "returned_letters": [l for l in letters if l["status"] == "returned"],
        "in_progress": [l for l in letters if l["status"] in
                        ("pending", "under_review", "waiting_signature", "processing")],
        "completed_docs": [l for l in letters if l["status"] == "fully_signed"],
        "declined_docs": [l for l in letters if l["status"] in ("disapproved", "expired")],
        "cancelled_docs": workflow.cancelled_letters_for_org(user["organization_id"]),
        "directory": workflow.office_directory(),
        "hierarchy": get_hierarchy(active_only=True),
    }


@app.route("/dashboard")
@roles_required("user")
def dashboard():
    user = current_user()
    return render_shell(user, "dashboard", "partials/dashboard.html", **build_dashboard_context(user))


@app.route("/p/dashboard")
@roles_required("user")
def dashboard_partial():
    user = current_user()
    return render_template("partials/dashboard.html", **build_dashboard_context(user))


@app.route("/api/directory")
@roles_required("user")  # availability & workload are shown only in the Organization portal
def directory_search():
    """Searchable offices / signatories with live availability (rows only)."""
    return render_template("partials/office_directory_rows.html",
                           directory=workflow.office_directory(request.args.get("q", "")))


@app.route("/api/pulse")
@login_required
def pulse():
    """Lightweight poll so dashboards/queues refresh automatically when something changes."""
    user = current_user()
    last_notif = query("SELECT COALESCE(MAX(id), 0) AS m FROM notifications WHERE user_id = ?",
                       (user["id"],), one=True)["m"]
    if user["role"] == "moderator":
        stamp = query("SELECT COALESCE(MAX(id), 0) AS m FROM activity_log", one=True)["m"]
    else:
        stamp = last_notif
    # new unread notifications since the last one this page has seen -> pop-up messages
    since = request.args.get("since", type=int)
    new_items = []
    if since is not None:
        new_items = [{"id": n["id"], "kind": n["kind"], "message": n["message"], "letter_id": n["letter_id"]}
                     for n in query("""SELECT id, kind, message, letter_id FROM notifications
                                        WHERE user_id = ? AND id > ? AND is_read = 0 ORDER BY id LIMIT 5""",
                                    (user["id"], since))]
    waiting = query("""SELECT COUNT(*) AS n FROM notifications
                        WHERE user_id = ? AND is_read = 0 AND kind IN ('queue', 'waiting_signature')""",
                    (user["id"],), one=True)["n"]
    return jsonify({"ok": True, "unread": workflow.unread_count(user["id"]), "stamp": stamp,
                    "last_notification": last_notif, "new": new_items, "unread_to_sign": waiting})


# ───────────── Signatures (all signing roles) ─────────────

@app.route("/upload-signature", methods=["GET", "POST"])
@roles_required("user", "office")
def upload_signature():
    user = current_user()
    settings_url = home_url_for(user) + "#settings"

    if request.method != "POST":
        return redirect(settings_url)

    def fail(msg):
        if is_ajax():
            return jsonify({"ok": False, "error": msg})
        flash(msg, "error")
        return redirect(settings_url)

    file = request.files.get("signature")
    if not file or not file.filename or not file.filename.strip():
        return fail("Please choose a PNG signature file.")
    if not file.filename.lower().endswith((".png", ".jpg", ".jpeg")):
        return fail("Signature must be a PNG image (.png). JPG is also accepted.")
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size == 0:
        return fail("The selected file is empty. Please choose a valid PNG signature.")

    remove_bg = request.form.get("remove_background") == "1"  # unchecked box -> keep image as uploaded
    filename = f"signature_user{user['id']}_{uuid.uuid4().hex[:8]}.png"
    path = os.path.join(UPLOAD_FOLDER, filename)
    try:
        pdfsign.prepare_signature_image(file.stream, path, remove_background=remove_bg)
    except Exception as e:
        return fail(str(e) if isinstance(e, ValueError) else "Failed to read the image. Please try another file.")

    old = user["signature_path"]
    with transaction():
        execute("UPDATE users SET signature_path = ?, signature_filename = ? WHERE id = ?",
                (path, filename, user["id"]))
        workflow.log_activity(user, "signature_updated", "Uploaded a new signature image")
    if old and old != path and os.path.basename(old).startswith(f"signature_user{user['id']}"):
        try:
            os.remove(old)  # signed letters keep their own snapshot copy
        except OSError:
            pass

    if is_ajax():
        return jsonify({"ok": True, "message": "Signature saved successfully."})
    flash("Signature saved successfully.", "success")
    return redirect(settings_url)


# ───────────── Letters: submit (organizations) ─────────────

def _upload_form_ctx(form=None, errors=None):
    return {
        "form": form or {"title": "", "priority": "low", "expiry_date": "", "note": ""},
        "errors": errors or {},
        "hierarchy": get_hierarchy(active_only=True),
    }


@app.route("/p/upload-document")
@roles_required("user")
def upload_document_partial():
    return render_template("partials/upload_document.html", **_upload_form_ctx())


@app.route("/upload-document", methods=["GET", "POST"])
@roles_required("user")
def upload_document():
    user = current_user()
    if request.method == "GET":
        return redirect(url_for("dashboard") + "#request_signature")

    form = {
        "title": request.form.get("title", "").strip(),
        "priority": request.form.get("priority", "low"),
        "expiry_date": request.form.get("expiry_date", "").strip(),
        "note": request.form.get("note", "").strip(),
        "office_ids": request.form.getlist("office_ids"),
    }
    errors = {}
    if not form["office_ids"]:
        errors["office_ids"] = "Please choose at least one signatory office."
    if not form["title"]:
        errors["title"] = "Title is required."
    if not form["expiry_date"]:
        errors["expiry_date"] = "Expiry date is required."
    file = request.files.get("document")
    if not file or not file.filename:
        errors["document"] = "Please choose a PDF."

    if not errors:
        try:
            workflow.submit_letter(user, form["title"], form["note"], form["expiry_date"],
                                   form["priority"], file, office_ids=form["office_ids"])
            flash("Letter submitted and routed to the first signatory office.", "success")
            return redirect(url_for("dashboard"))
        except WorkflowError as e:
            msg = str(e)
            low = msg.lower()
            key = ("expiry_date" if "expiry" in low else "document" if "pdf" in low
                   else "office_ids" if ("office" in low or "signatory" in low) else "general")
            errors[key] = msg

    return render_shell(user, "request_signature", "partials/upload_document.html",
                        **_upload_form_ctx(form, errors))


# ───────────── Letters: details / files ─────────────

def _load_letter_or_404(letter_id):
    user = current_user()
    letter = find_letter_by_id(letter_id)
    access = workflow.letter_access(user, letter)
    if not access:
        # same response whether it doesn't exist or isn't assigned yet: no information leak
        abort(404)
    return user, letter, access


@app.route("/p/document/<int:letter_id>")
@login_required
def document_view(letter_id):
    """Letter details + tracker + comments; for the current office also the review/sign actions."""
    user, letter, access = _load_letter_or_404(letter_id)
    if access == "office":
        workflow.open_letter(user, letter_id)
        letter = find_letter_by_id(letter_id)

    steps = get_steps(letter_id, letter["current_cycle"])
    progress = workflow.letter_progress(letter, steps)
    current_step = next((s for s in steps if s["status"] in ("queued", "processing", "awaiting_signature")), None)
    can_act = (access == "office" and current_step is not None
               and current_step["office_id"] == user["office_id"]
               and letter["status"] in ("pending", "under_review", "waiting_signature", "processing"))
    office = None
    can_sign = False
    if can_act:
        from models import find_office_by_id
        office = find_office_by_id(user["office_id"])
        can_sign = workflow.office_signatory_ok(user, office)

    history_cycles = []
    if access in ("owner", "moderator"):
        for c in range(letter["current_cycle"] - 1, 0, -1):
            history_cycles.append({"cycle": c, "steps": get_steps(letter_id, c)})

    return render_template(
        "partials/document_view.html",
        doc=letter,
        access=access,
        progress=progress,
        steps=steps,
        current_step=current_step,
        can_act=can_act,
        can_sign=can_sign,
        office=office,
        comments=get_comments(letter_id),
        versions=get_versions(letter_id) if access in ("owner", "moderator") else [],
        history_cycles=history_cycles,
        activity=get_letter_activity(letter_id) if access == "moderator" else [],
        can_resubmit=(access == "owner" and letter["status"] in REVISABLE_LETTER_STATUSES),
        today=date.today().isoformat(),
        has_signature=bool(user["signature_filename"]),
        signature_url=url_for("uploaded_file", filename=user["signature_filename"])
        if user["signature_filename"] else None,
        pdf_url=url_for("document_file", letter_id=letter_id) if access in ("owner", "office") else None,
    )


@app.route("/letters/<int:letter_id>/file")
@login_required
def document_file(letter_id):
    """Inline PDF. Owner: any version. Office: only once the letter has reached it (current cycle)."""
    user, letter, access = _load_letter_or_404(letter_id)
    if access not in ("owner", "office"):
        abort(404)
    version_id = request.args.get("version", type=int)
    if version_id:
        if access != "owner":
            abort(404)
        v = query("SELECT * FROM letter_versions WHERE id = ? AND letter_id = ?", (version_id, letter_id), one=True)
        if not v:
            abort(404)
        path = v["file_path"]
    elif letter["status"] == "fully_signed" and letter["final_file_path"]:
        path = letter["final_file_path"]
    else:
        v = query("SELECT * FROM letter_versions WHERE id = ?", (letter["current_version_id"],), one=True)
        path = letter["working_file_path"] or v["file_path"]
    if not path or not os.path.isfile(path):
        abort(404)
    resp = send_file(path, mimetype="application/pdf")
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.route("/document/<int:doc_id>/signed-download")
@login_required
def document_signed_download(doc_id):
    """Final signed PDF with its tracking number (always the same file; number never re-issued)."""
    user, letter, access = _load_letter_or_404(doc_id)
    if access not in ("owner", "office") or letter["status"] != "fully_signed" or not letter["final_file_path"]:
        flash("The document is not fully signed yet.", "error")
        return redirect(home_url_for(user))
    if not os.path.isfile(letter["final_file_path"]):
        abort(404)
    with transaction():
        workflow.log_activity(user, "final_downloaded", f"Downloaded {letter['tracking_number']}",
                              letter_id=doc_id)
    safe_title = re.sub(r"[^A-Za-z0-9 _-]+", "", letter["title"]).strip()[:60] or "letter"
    resp = send_file(letter["final_file_path"], as_attachment=True,
                     download_name=f"{letter['tracking_number']} - {safe_title}.pdf")
    resp.headers["Cache-Control"] = "private, max-age=0"
    return resp


# ───────────── Letters: actions ─────────────

def _done(ok_msg, user, error=None):
    if is_ajax():
        if error:
            return jsonify({"ok": False, "error": error}), 400
        return jsonify({"ok": True, "message": ok_msg})
    flash(error or ok_msg, "error" if error else "success")
    return redirect(home_url_for(user))


@app.route("/letters/<int:letter_id>/comment", methods=["POST"])
@roles_required("office")
def letter_comment(letter_id):
    user = current_user()
    try:
        workflow.add_comment(user, letter_id, request.form.get("body", ""))
    except WorkflowError as e:
        return _done(None, user, str(e))
    return _done("Remark added.", user)


@app.route("/document/<int:letter_id>/action", methods=["POST"])
@roles_required("office")
def document_action(letter_id):
    """Approve & Sign / Approve (endorse) / Return for Revision / Disapprove."""
    user = current_user()
    action = request.form.get("action")
    remarks = request.form.get("remarks", "") or request.form.get("reason", "")
    try:
        if action == "approve_sign":
            workflow.approve_and_sign(
                user, letter_id,
                request.form.get("page_index", "0"),
                request.form.get("rel_x"), request.form.get("rel_y"), request.form.get("rel_w"),
                remarks,
            )
            msg = "Letter approved and signed."
        elif action == "endorse":
            workflow.endorse_letter(user, letter_id, remarks)
            msg = "Letter approved and forwarded to your office signatory."
        elif action in ("return", "disapprove"):
            workflow.return_or_disapprove(user, letter_id,
                                          "returned" if action == "return" else "disapproved", remarks)
            msg = "Letter returned for revision." if action == "return" else "Letter disapproved."
        else:
            return _done(None, user, "Unknown action.")
    except WorkflowError as e:
        return _done(None, user, str(e))
    return _done(msg, user)


@app.route("/letters/<int:letter_id>/resubmit", methods=["POST"])
@roles_required("user")
def letter_resubmit(letter_id):
    user = current_user()
    try:
        workflow.resubmit_letter(
            user, letter_id,
            request.form.get("title", ""), request.form.get("note", ""),
            request.form.get("expiry_date", ""), request.files.get("document"),
            request.form.get("remarks", ""),
        )
    except WorkflowError as e:
        return _done(None, user, str(e))
    return _done("Letter resubmitted. Signing restarts from the first office.", user)


@app.route("/document/<int:doc_id>/delete", methods=["POST"])
@roles_required("user")
def delete_document(doc_id):
    """Withdraw a letter. Records are kept for the audit trail (soft delete)."""
    user = current_user()
    try:
        workflow.withdraw_letter(user, doc_id)
    except WorkflowError as e:
        return _done(None, user, str(e))
    return _done("Signing request deleted.", user)


# ───────────── Notifications ─────────────

@app.route("/p/notifications")
@login_required
def notifications_partial():
    user = current_user()
    items = query(
        """SELECT n.*, l.title AS letter_title FROM notifications n
             LEFT JOIN letters l ON l.id = n.letter_id
            WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 200""",
        (user["id"],))
    html = render_template("partials/notifications.html", items=items)
    with transaction():
        execute("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0", (user["id"],))
    return html


# ───────────── Settings / contacts ─────────────

@app.route("/p/settings")
@roles_required("user", "office", "moderator")
def settings_partial():
    return render_template("partials/settings.html", user=current_user())


@app.route("/p/contacts")
@login_required
def contacts_partial():
    return render_template("partials/contacts.html",
                           contact_rows=parse_contact_rows(get_content("contacts_rows")))


# ───────────── SELF-SIGN (personal; separate from the letter workflow) ─────────────

SELF_SIGN_TEMP: dict = {}


@app.route("/p/sign-yourself")
@roles_required("user", "office")
def sign_yourself_partial():
    user = current_user()
    docs = query("SELECT * FROM self_sign_docs WHERE owner_id = ? ORDER BY id DESC", (user["id"],))
    return render_template("partials/sign_yourself.html", docs=docs)


@app.route("/self-sign-temp-upload", methods=["POST"])
@roles_required("user", "office")
def self_sign_temp_upload():
    user = current_user()
    if not user["signature_path"]:
        return jsonify({"ok": False, "error": "You must upload a signature in Settings first."})
    file = request.files.get("document")
    if not file or file.filename == "":
        return jsonify({"ok": False, "error": "Please choose a PDF file."})
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"ok": False, "error": "Only PDF files (.pdf) are allowed."})

    original_name = os.path.basename(file.filename)
    raw_name = f"self_raw_{user['id']}_{uuid.uuid4().hex[:8]}.pdf"
    raw_path = os.path.join(UPLOAD_FOLDER, raw_name)
    file.save(raw_path)
    try:
        pdfsign.page_count(raw_path)
    except Exception:
        os.remove(raw_path)
        return jsonify({"ok": False, "error": "The uploaded file is not a valid PDF."})

    temp_id = uuid.uuid4().hex
    title, _ = splitext(original_name)
    SELF_SIGN_TEMP[temp_id] = {
        "owner_id": user["id"], "raw_path": raw_path,
        "original_name": original_name, "title": title or "Self-sign document",
    }
    return jsonify({
        "ok": True,
        "temp_id": temp_id,
        "pdf_url": url_for("uploaded_file", filename=raw_name),
        "signature_url": url_for("uploaded_file", filename=user["signature_filename"]),
    })


@app.route("/self-sign/finalize", methods=["POST"])
@roles_required("user", "office")
def self_sign_finalize():
    user = current_user()
    back = home_url_for(user) + "#sign_yourself"
    temp = SELF_SIGN_TEMP.get(request.form.get("temp_id", ""))
    if not temp or temp["owner_id"] != user["id"]:
        flash("Self-sign session not found or expired. Please upload again.", "error")
        return redirect(back)
    if not user["signature_path"]:
        flash("Please upload your signature in Settings before self-signing a document.", "error")
        return redirect(home_url_for(user) + "#settings")
    try:
        page_index = -1 if request.form.get("apply_to") == "all" else int(request.form.get("page_index", "0"))
        rel = (page_index, float(request.form["rel_x"]), float(request.form["rel_y"]),
               float(request.form["rel_w"]))
    except (KeyError, TypeError, ValueError):
        flash("Please drag the signature on the preview to position it before saving.", "error")
        return redirect(back)

    signed_path = _create_signed_pdf(temp["raw_path"], user["signature_path"], rel_coord=rel,
                                     out_name=f"signed_{os.path.basename(temp['raw_path'])}")
    if not signed_path:
        flash("Failed to create signed PDF.", "error")
        return redirect(back)

    with transaction():
        execute("""INSERT INTO self_sign_docs(owner_id, title, signed_path, original_path, original_name, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user["id"], temp["title"], signed_path, temp["raw_path"], temp["original_name"], now_str()))
        workflow.log_activity(user, "self_signed", f"Self-signed '{temp['original_name']}'")
    SELF_SIGN_TEMP.pop(request.form.get("temp_id", ""), None)
    flash("Self-signed PDF created and saved under Your Self-Signed PDFs.", "success")
    return redirect(back)


@app.route("/self-sign/<int:doc_id>/download")
@roles_required("user", "office")
def self_sign_download(doc_id):
    user = current_user()
    doc = query("SELECT * FROM self_sign_docs WHERE id = ? AND owner_id = ?", (doc_id, user["id"]), one=True)
    if not doc or not os.path.isfile(doc["signed_path"]):
        abort(404)
    return send_file(doc["signed_path"], as_attachment=True,
                     download_name=f"signed_{doc['original_name']}")


# ───────────── Portals ─────────────

from routes_admin import bp as admin_bp  # noqa: E402
from routes_moderator import bp as moderator_bp  # noqa: E402

app.register_blueprint(admin_bp)
app.register_blueprint(moderator_bp)


if __name__ == "__main__":
    app.run(debug=True)
