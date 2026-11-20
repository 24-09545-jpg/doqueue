# workflow.py
"""
Letter routing / signing workflow for the DSA System.

All rules are enforced here (server side), never only in templates:
  * a letter is routed through the signing hierarchy one office at a time,
  * only the office currently holding the letter may open / comment / act,
  * offices further down the route cannot see the letter until it reaches them,
  * a disapproval or return stops the route, resets progress and keeps history,
  * a resubmission starts a new routing cycle from the first office,
  * the tracking number is issued from one central counter, inside the same
    write transaction that completes the final signature.
"""
import os
import shutil
import uuid
from datetime import date, datetime

import pdfsign
from db import AVAILABILITY_CHOICES, execute, now_str, query, transaction
from models import (
    ACTIVE_STEP_STATUSES, IN_FLIGHT_LETTER_STATUSES, LETTER_STATUS_LABELS,
    REVISABLE_LETTER_STATUSES, STEP_STATUS_LABELS,
    find_letter_by_id, find_office_by_id, get_current_step, get_hierarchy,
    get_signatures, get_steps, get_version, moderator_ids,
    user_ids_for_office, user_ids_for_organization,
)

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")


class WorkflowError(Exception):
    """An action that is not allowed; the message is safe to show to the user."""


def configure(upload_folder):
    global UPLOAD_FOLDER
    UPLOAD_FOLDER = upload_folder
    for sub in ("letters", "signatures", "final"):
        os.makedirs(os.path.join(UPLOAD_FOLDER, sub), exist_ok=True)


def _path(sub, name):
    return os.path.join(UPLOAD_FOLDER, sub, name)


# ───────────── audit log & notifications ─────────────

def log_activity(actor, action, details="", letter_id=None, office_id=None):
    ip = ""
    try:
        from flask import has_request_context, request
        if has_request_context():
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")[:64]
    except Exception:
        pass
    execute(
        """INSERT INTO activity_log(actor_user_id, actor_name, actor_role, office_id, letter_id,
                                    action, details, ip, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            actor["id"] if actor else None,
            actor["name"] if actor else "System",
            actor["role"] if actor else "system",
            office_id if office_id is not None else (actor.get("office_id") if actor else None),
            letter_id, action, details or "", ip, now_str(),
        ),
    )


def notify(user_ids, message, kind, letter_id=None):
    ts = now_str()
    for uid in sorted(set(u for u in user_ids if u)):
        execute(
            "INSERT INTO notifications(user_id, letter_id, kind, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (uid, letter_id, kind, message, ts),
        )


def unread_count(user_id):
    row = query("SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND is_read = 0", (user_id,), one=True)
    return row["n"] if row else 0


# ───────────── helpers ─────────────

def _require_pdf(file_storage):
    if not file_storage or not file_storage.filename:
        raise WorkflowError("Please choose a PDF.")
    if not file_storage.filename.lower().endswith(".pdf"):
        raise WorkflowError("Only PDF files (.pdf) are allowed.")


def _store_pdf(file_storage, prefix):
    """Save an uploaded PDF under uploads/letters with a random name; verify it parses."""
    _require_pdf(file_storage)
    name = f"{prefix}_{uuid.uuid4().hex}.pdf"
    path = _path("letters", name)
    file_storage.save(path)
    try:
        if pdfsign.page_count(path) < 1:
            raise ValueError
    except Exception:
        _safe_remove(path)
        raise WorkflowError("The uploaded file is not a valid PDF.")
    return path


def _safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _parse_date(raw):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def status_label(status):
    return LETTER_STATUS_LABELS.get(status, (status or "").replace("_", " ").title())


def office_signatory_ok(user, office):
    """
    Signing rights inside an office: if the moderator designated an (active)
    signatory, only that account may apply the office signature; staff
    accounts may still review, comment, endorse or return.
    """
    designated = office["signatory_user_id"] if office and office["signatory_active"] else None
    return designated is None or designated == user["id"]


def _load_for_office_action(user, letter_id, allowed_step_statuses=ACTIVE_STEP_STATUSES):
    """Re-read letter + current step inside the transaction and check the actor may act."""
    if not user or user["role"] != "office" or not user["office_id"]:
        raise WorkflowError("Only signatory office accounts can process letters.")
    letter = find_letter_by_id(letter_id)
    if not letter or letter["is_withdrawn"]:
        raise WorkflowError("Letter not found.")
    if letter["status"] == "expired":
        raise WorkflowError("This letter has passed its expiry date and can no longer be processed.")
    if letter["status"] not in IN_FLIGHT_LETTER_STATUSES:
        raise WorkflowError(f"This letter is {status_label(letter['status'])} and cannot be processed.")
    step = get_current_step(letter_id, letter["current_cycle"])
    if not step or step["office_id"] != user["office_id"]:
        raise WorkflowError("This letter is not currently assigned to your office.")
    if step["status"] not in allowed_step_statuses:
        raise WorkflowError("This letter is not in a state that allows this action.")
    office = find_office_by_id(user["office_id"])
    if not office or not office["is_active"]:
        raise WorkflowError("Your office is currently deactivated.")
    return letter, step, office


def recompute_letter_status(letter_id):
    """Derive the overall status from the current cycle's steps (in-flight letters only)."""
    letter = query("SELECT * FROM letters WHERE id = ?", (letter_id,), one=True)
    if not letter or letter["status"] not in IN_FLIGHT_LETTER_STATUSES:
        return letter["status"] if letter else None
    steps = get_steps(letter_id, letter["current_cycle"])
    current = next((s for s in steps if s["status"] in ACTIVE_STEP_STATUSES), None)
    if current is None:
        return letter["status"]
    if current["status"] == "processing":
        status = "under_review"
    elif current["status"] == "awaiting_signature":
        status = "waiting_signature"
    elif any(s["status"] == "completed" for s in steps):
        status = "processing"
    else:
        status = "pending"
    execute("UPDATE letters SET status = ?, updated_at = ? WHERE id = ?", (status, now_str(), letter_id))
    return status


def _route_for(office_ids):
    """
    The offices that will sign, always in official hierarchy order.
    `office_ids` = the organization's selection (None = every office in the hierarchy).
    """
    hierarchy = get_hierarchy(active_only=True)
    if not hierarchy:
        raise WorkflowError("No signing hierarchy has been configured yet. Please contact the DSA.")
    if office_ids is None:
        return hierarchy
    try:
        wanted = {int(i) for i in office_ids}
    except (TypeError, ValueError):
        raise WorkflowError("Invalid signatory selection.")
    valid = {o["id"] for o in hierarchy}
    if not wanted:
        raise WorkflowError("Please choose at least one signatory office.")
    if not wanted <= valid:
        raise WorkflowError("One of the chosen offices is not part of the signing hierarchy.")
    return [o for o in hierarchy if o["id"] in wanted]


def _create_cycle_steps(letter_id, cycle, version_id, office_ids=None):
    hierarchy = _route_for(office_ids)
    ts = now_str()
    for idx, office in enumerate(hierarchy, start=1):
        execute(
            """INSERT INTO letter_steps(letter_id, cycle, version_id, step_order, office_id, office_name,
                                        status, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (letter_id, cycle, version_id, idx, office["id"], office["name"],
             "queued" if idx == 1 else "waiting", ts if idx == 1 else None),
        )
    return hierarchy[0]


def _add_comment(letter, step, actor, kind, body, office=None):
    execute(
        """INSERT INTO letter_comments(letter_id, cycle, step_id, user_id, office_id, author_name,
                                       office_name, kind, body, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (letter["id"], letter["current_cycle"], step["id"] if step else None, actor["id"],
         office["id"] if office else None, actor["name"], office["name"] if office else
         (actor.get("organization_name") or ""), kind, body.strip(), now_str()),
    )


# ───────────── organization actions ─────────────

def submit_letter(user, title, note, expiry_raw, priority, file_storage, office_ids=None):
    if not user or user["role"] != "user" or not user["organization_id"]:
        raise WorkflowError("Only organization accounts can submit letters.")
    title = (title or "").strip()
    if not title:
        raise WorkflowError("Title is required.")
    exp = _parse_date(expiry_raw)
    if not exp:
        raise WorkflowError("Please provide a valid expiry date.")
    if exp < date.today():
        raise WorkflowError("Expiry date cannot be in the past.")
    _route_for(office_ids)  # validate the selection before saving the file
    priority = priority if priority in ("low", "medium", "high") else "low"

    path = _store_pdf(file_storage, f"org{user['organization_id']}")
    try:
        with transaction():
            ts = now_str()
            letter_id = execute(
                """INSERT INTO letters(organization_id, submitted_by, title, note, priority, expiry_date,
                                       status, current_cycle, created_at, submitted_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?)""",
                (user["organization_id"], user["id"], title, (note or "").strip(), priority,
                 exp.isoformat(), ts, ts, ts),
            )
            version_id = execute(
                """INSERT INTO letter_versions(letter_id, version_no, file_path, original_filename,
                                               uploaded_by, remarks, created_at)
                   VALUES (?, 1, ?, ?, ?, 'Initial submission', ?)""",
                (letter_id, path, file_storage.filename, user["id"], ts),
            )
            execute("UPDATE letters SET current_version_id = ? WHERE id = ?", (version_id, letter_id))
            first = _create_cycle_steps(letter_id, 1, version_id, office_ids)

            log_activity(user, "letter_submitted", f"Submitted '{title}'", letter_id=letter_id)
            log_activity(None, "letter_assigned", f"Assigned to {first['name']} (step 1)",
                         letter_id=letter_id, office_id=first["id"])
            org = user.get("organization_name") or "an organization"
            notify(user_ids_for_organization(user["organization_id"]),
                   f"Your letter '{title}' was submitted and assigned to {first['name']}.",
                   "submitted", letter_id)
            notify(user_ids_for_office(first["id"]),
                   f"New letter '{title}' from {org} entered your queue.", "queue", letter_id)
            return letter_id
    except Exception:
        _safe_remove(path)
        raise


def resubmit_letter(user, letter_id, title, note, expiry_raw, file_storage, remarks):
    if not user or user["role"] != "user":
        raise WorkflowError("Only organization accounts can resubmit letters.")
    letter = find_letter_by_id(letter_id)
    if not letter or letter["organization_id"] != user["organization_id"] or letter["is_withdrawn"]:
        raise WorkflowError("Letter not found.")
    if letter["status"] not in REVISABLE_LETTER_STATUSES:
        raise WorkflowError("Only returned, disapproved or expired letters can be resubmitted.")
    title = (title or "").strip() or letter["title"]
    exp = _parse_date(expiry_raw) if expiry_raw else _parse_date(letter["expiry_date"])
    if not exp or exp < date.today():
        raise WorkflowError("Please provide a valid expiry date that is not in the past.")

    new_path = None
    if file_storage and file_storage.filename:
        new_path = _store_pdf(file_storage, f"org{user['organization_id']}")
    try:
        with transaction():
            letter = find_letter_by_id(letter_id)  # re-read under lock
            if letter["status"] not in REVISABLE_LETTER_STATUSES:
                raise WorkflowError("This letter was already resubmitted.")
            prev = get_version(letter["current_version_id"])
            ts = now_str()
            vno = query("SELECT COALESCE(MAX(version_no), 0) + 1 AS n FROM letter_versions WHERE letter_id = ?",
                        (letter_id,), one=True)["n"]
            version_id = execute(
                """INSERT INTO letter_versions(letter_id, version_no, file_path, original_filename,
                                               uploaded_by, remarks, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (letter_id, vno, new_path or prev["file_path"],
                 file_storage.filename if new_path else prev["original_filename"],
                 user["id"], (remarks or "").strip() or
                 ("Revised file uploaded" if new_path else "Resubmitted (details edited, same file)"), ts),
            )
            cycle = letter["current_cycle"] + 1
            execute(
                """UPDATE letters SET title = ?, note = ?, expiry_date = ?, status = 'pending',
                          current_cycle = ?, current_version_id = ?, working_file_path = NULL,
                          submitted_at = ?, updated_at = ?
                    WHERE id = ?""",
                (title, (note or "").strip(), exp.isoformat(), cycle, version_id, ts, ts, letter_id),
            )
            letter = find_letter_by_id(letter_id)
            # keep the organization's chosen signatories (those still in the hierarchy)
            active = {o["id"] for o in get_hierarchy(active_only=True)}
            chosen = [r["office_id"] for r in query(
                "SELECT office_id FROM letter_steps WHERE letter_id = ? AND cycle = ?", (letter_id, cycle - 1))
                if r["office_id"] in active]
            first = _create_cycle_steps(letter_id, cycle, version_id, chosen or None)
            if remarks and remarks.strip():
                _add_comment(letter, None, user, "resubmission", remarks)

            log_activity(user, "letter_resubmitted",
                         f"Resubmitted as version {vno} (routing cycle {cycle})", letter_id=letter_id)
            log_activity(None, "letter_assigned", f"Assigned to {first['name']} (step 1)",
                         letter_id=letter_id, office_id=first["id"])
            notify(user_ids_for_organization(letter["organization_id"]),
                   f"Your revised letter '{title}' was resubmitted and assigned to {first['name']}.",
                   "resubmitted", letter_id)
            notify(user_ids_for_office(first["id"]),
                   f"Revised letter '{title}' from {letter['organization_name']} entered your queue.",
                   "queue", letter_id)
    except Exception:
        _safe_remove(new_path)
        raise


def withdraw_letter(user, letter_id):
    with transaction():
        letter = find_letter_by_id(letter_id)
        if not letter or user["role"] != "user" or letter["organization_id"] != user["organization_id"]:
            raise WorkflowError("Letter not found.")
        if letter["status"] == "fully_signed" or letter["is_withdrawn"]:
            raise WorkflowError("This letter can no longer be withdrawn.")
        step = get_current_step(letter_id, letter["current_cycle"])
        execute("""UPDATE letter_steps SET status = 'cancelled'
                    WHERE letter_id = ? AND cycle = ? AND status IN ('waiting','queued','processing','awaiting_signature')""",
                (letter_id, letter["current_cycle"]))
        execute("UPDATE letters SET is_withdrawn = 1, status = 'withdrawn', updated_at = ? WHERE id = ?",
                (now_str(), letter_id))
        log_activity(user, "letter_withdrawn", f"Withdrew '{letter['title']}'", letter_id=letter_id)
        if step:
            notify(user_ids_for_office(step["office_id"]),
                   f"Letter '{letter['title']}' was withdrawn by {letter['organization_name']}.",
                   "withdrawn", letter_id)


# ───────────── office actions ─────────────

def open_letter(user, letter_id):
    """Office opens a queued letter -> 'Currently Being Processed' / 'Under Review'."""
    if not user or user["role"] != "office":
        return
    with transaction():
        letter = find_letter_by_id(letter_id)
        if not letter or letter["status"] not in IN_FLIGHT_LETTER_STATUSES:
            return
        step = get_current_step(letter_id, letter["current_cycle"])
        if not step or step["office_id"] != user["office_id"] or step["status"] != "queued":
            return
        execute("UPDATE letter_steps SET status = 'processing', opened_at = ? WHERE id = ?",
                (now_str(), step["id"]))
        recompute_letter_status(letter_id)
        log_activity(user, "letter_opened", f"{step['office_name']} started reviewing", letter_id=letter_id)
        notify(user_ids_for_organization(letter["organization_id"]),
               f"Your letter '{letter['title']}' is now under review by {step['office_name']}.",
               "under_review", letter_id)


def add_comment(user, letter_id, body):
    body = (body or "").strip()
    if not body:
        raise WorkflowError("Comment cannot be empty.")
    if len(body) > 4000:
        raise WorkflowError("Comment is too long (max 4000 characters).")
    with transaction():
        letter, step, office = _load_for_office_action(user, letter_id)
        _add_comment(letter, step, user, "comment", body, office)
        log_activity(user, "comment_added", body[:200], letter_id=letter_id)
        notify(user_ids_for_organization(letter["organization_id"]),
               f"{office['name']} added a remark on your letter '{letter['title']}'.",
               "comment", letter_id)


def endorse_letter(user, letter_id, remarks):
    """A non-designated office account approves; the designated signatory must then sign."""
    with transaction():
        letter, step, office = _load_for_office_action(user, letter_id, ("queued", "processing"))
        execute("""UPDATE letter_steps SET status = 'awaiting_signature', endorsed_by = ?, endorsed_at = ?,
                          opened_at = COALESCE(opened_at, ?)
                    WHERE id = ?""", (user["id"], now_str(), now_str(), step["id"]))
        if remarks and remarks.strip():
            _add_comment(letter, step, user, "approval", remarks, office)
        recompute_letter_status(letter_id)
        log_activity(user, "letter_endorsed", "Reviewed and approved; waiting for the office signatory",
                     letter_id=letter_id)
        signers = ([office["signatory_user_id"]] if office["signatory_user_id"] and office["signatory_active"]
                   else user_ids_for_office(office["id"]))
        notify(signers,
               f"Letter '{letter['title']}' was approved by {user['name']} and is waiting for your signature.",
               "waiting_signature", letter_id)
        notify(user_ids_for_organization(letter["organization_id"]),
               f"Your letter '{letter['title']}' was approved by {office['name']} and is waiting for signature.",
               "approved", letter_id)


def approve_and_sign(user, letter_id, page_index, rel_x, rel_y, rel_w, remarks=""):
    if not user.get("signature_path") or not os.path.exists(user["signature_path"]):
        raise WorkflowError("You must upload a signature in Settings before signing.")
    try:
        page_index, rel_x, rel_y, rel_w = int(page_index), float(rel_x), float(rel_y), float(rel_w)
    except (TypeError, ValueError):
        raise WorkflowError("Please place your signature on the document before submitting.")

    created = []
    old_working = None
    try:
        with transaction():
            letter, step, office = _load_for_office_action(user, letter_id)
            if not office_signatory_ok(user, office):
                raise WorkflowError("Only the designated signatory of your office can apply the signature. "
                                    "Use 'Approve' to forward it to them.")
            version = get_version(letter["current_version_id"])

            # snapshot the signature image so later signature changes never alter signed letters
            sig_copy = _path("signatures", f"sig_l{letter_id}_s{step['id']}_{uuid.uuid4().hex[:8]}.png")
            shutil.copyfile(user["signature_path"], sig_copy)
            created.append(sig_copy)

            try:
                placement = pdfsign.normalize_placement(version["file_path"], sig_copy,
                                                        page_index, rel_x, rel_y, rel_w)
            except ValueError as e:
                raise WorkflowError(str(e) or "Invalid signature placement.")

            ts = now_str()
            execute(
                """INSERT INTO letter_signatures(letter_id, step_id, cycle, user_id, office_id, signer_name,
                                                 office_name, image_path, page_index, rel_x, rel_y, rel_w,
                                                 rel_h, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (letter_id, step["id"], letter["current_cycle"], user["id"], office["id"], user["name"],
                 office["name"], sig_copy, placement["page_index"], placement["rel_x"], placement["rel_y"],
                 placement["rel_w"], placement["rel_h"], ts),
            )
            if remarks and remarks.strip():
                _add_comment(letter, step, user, "approval", remarks, office)
            execute("""UPDATE letter_steps SET status = 'completed', acted_by = ?, acted_at = ?,
                              opened_at = COALESCE(opened_at, ?)
                        WHERE id = ?""", (user["id"], ts, ts, step["id"]))

            # re-render the working copy (original version + every signature of this cycle)
            sigs = get_signatures(letter_id, letter["current_cycle"])
            working = _path("letters", f"working_l{letter_id}_c{letter['current_cycle']}_{uuid.uuid4().hex[:8]}.pdf")
            pdfsign.render_pdf(version["file_path"], sigs, working)
            created.append(working)
            old_working = letter["working_file_path"]
            execute("UPDATE letters SET working_file_path = ?, updated_at = ? WHERE id = ?",
                    (working, ts, letter_id))
            log_activity(user, "letter_signed", f"Approved and signed for {office['name']}", letter_id=letter_id)

            nxt = query(
                """SELECT * FROM letter_steps WHERE letter_id = ? AND cycle = ? AND status = 'waiting'
                    ORDER BY step_order LIMIT 1""",
                (letter_id, letter["current_cycle"]), one=True)
            org_users = user_ids_for_organization(letter["organization_id"])

            if nxt:
                execute("UPDATE letter_steps SET status = 'queued', received_at = ? WHERE id = ?", (ts, nxt["id"]))
                recompute_letter_status(letter_id)
                log_activity(None, "letter_assigned",
                             f"Forwarded to {nxt['office_name']} (step {nxt['step_order']})",
                             letter_id=letter_id, office_id=nxt["office_id"])
                notify(org_users,
                       f"{office['name']} signed your letter '{letter['title']}'. "
                       f"It was forwarded to {nxt['office_name']}.", "forwarded", letter_id)
                notify(user_ids_for_office(nxt["office_id"]),
                       f"New letter '{letter['title']}' from {letter['organization_name']} entered your queue.",
                       "queue", letter_id)
            else:
                tracking = _finalize(letter, version, sigs, ts, created)
                participating = query(
                    "SELECT DISTINCT office_id FROM letter_steps WHERE letter_id = ? AND cycle = ?",
                    (letter_id, letter["current_cycle"]))
                notify(org_users,
                       f"Your letter '{letter['title']}' is fully signed. Tracking No. {tracking}.",
                       "fully_signed", letter_id)
                for row in participating:
                    notify(user_ids_for_office(row["office_id"]),
                           f"Letter '{letter['title']}' completed all signatures ({tracking}).",
                           "fully_signed", letter_id)
    except Exception:
        for p in created:
            _safe_remove(p)
        raise
    if old_working:
        _safe_remove(old_working)


def _finalize(letter, version, sigs, ts, created):
    """Issue the next central tracking number and render the final PDF (inside the caller's transaction)."""
    execute("UPDATE counters SET value = value + 1 WHERE name = 'tracking'")
    seq = query("SELECT value FROM counters WHERE name = 'tracking'", one=True)["value"]
    tracking = f"DSA-{datetime.now().year}-{seq:04d}"
    final_path = _path("final", f"final_l{letter['id']}_{uuid.uuid4().hex[:8]}.pdf")
    pdfsign.render_pdf(version["file_path"], sigs, final_path, tracking_number=tracking)
    created.append(final_path)
    execute(
        """UPDATE letters SET status = 'fully_signed', tracking_seq = ?, tracking_number = ?,
                  final_file_path = ?, completed_at = ?, updated_at = ?
            WHERE id = ? AND tracking_number IS NULL""",
        (seq, tracking, final_path, ts, ts, letter["id"]),
    )
    log_activity(None, "letter_fully_signed", f"All signatures completed. Tracking No. {tracking}",
                 letter_id=letter["id"])
    return tracking


def return_or_disapprove(user, letter_id, decision, reason):
    if decision not in ("returned", "disapproved"):
        raise WorkflowError("Invalid decision.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise WorkflowError("Please provide a reason so the organization knows what to revise.")
    with transaction():
        letter, step, office = _load_for_office_action(user, letter_id)
        ts = now_str()
        execute("""UPDATE letter_steps SET status = ?, acted_by = ?, acted_at = ?,
                          opened_at = COALESCE(opened_at, ?)
                    WHERE id = ?""", (decision, user["id"], ts, ts, step["id"]))
        execute("UPDATE letter_steps SET status = 'cancelled' WHERE letter_id = ? AND cycle = ? AND status = 'waiting'",
                (letter_id, letter["current_cycle"]))
        # progress resets: the whole route restarts on resubmission; history rows are kept
        execute("UPDATE letters SET status = ?, working_file_path = NULL, updated_at = ? WHERE id = ?",
                (decision, ts, letter_id))
        _add_comment(letter, step, user, decision, reason, office)

        verb = "disapproved" if decision == "disapproved" else "returned for revision"
        log_activity(user, f"letter_{decision}", f"{office['name']}: {reason[:200]}", letter_id=letter_id)
        notify(user_ids_for_organization(letter["organization_id"]),
               f"Your letter '{letter['title']}' was {verb} by {office['name']}. Reason: {reason[:150]}",
               decision, letter_id)
        signed_before = query(
            """SELECT DISTINCT office_id FROM letter_steps
                WHERE letter_id = ? AND cycle = ? AND status = 'completed'""",
            (letter_id, letter["current_cycle"]))
        for row in signed_before:
            notify(user_ids_for_office(row["office_id"]),
                   f"Letter '{letter['title']}' was {verb} by {office['name']}; signing will restart "
                   f"after the organization resubmits.", decision, letter_id)


# ───────────── expiry ─────────────

def expire_overdue_letters():
    """Move in-flight letters past their expiry date to 'expired' (they can be revised & resubmitted)."""
    today = date.today().isoformat()
    overdue = query(
        f"""SELECT id FROM letters WHERE is_withdrawn = 0 AND expiry_date IS NOT NULL AND expiry_date < ?
              AND status IN ({','.join('?' * len(IN_FLIGHT_LETTER_STATUSES))})""",
        (today, *IN_FLIGHT_LETTER_STATUSES))
    for row in overdue:
        with transaction():
            letter = find_letter_by_id(row["id"])
            if letter["status"] not in IN_FLIGHT_LETTER_STATUSES:
                continue
            step = get_current_step(letter["id"], letter["current_cycle"])
            execute("""UPDATE letter_steps SET status = 'cancelled'
                        WHERE letter_id = ? AND cycle = ? AND status IN ('waiting','queued','processing','awaiting_signature')""",
                    (letter["id"], letter["current_cycle"]))
            execute("UPDATE letters SET status = 'expired', working_file_path = NULL, updated_at = ? WHERE id = ?",
                    (now_str(), letter["id"]))
            log_activity(None, "letter_expired", f"Expiry date {letter['expiry_date']} passed", letter_id=letter["id"])
            notify(user_ids_for_organization(letter["organization_id"]),
                   f"Your letter '{letter['title']}' expired before all signatures were completed. "
                   f"You can revise the expiry date and resubmit it.", "expired", letter["id"])
            if step:
                notify(user_ids_for_office(step["office_id"]),
                       f"Letter '{letter['title']}' expired and was removed from your queue.", "expired", letter["id"])


# ───────────── read models ─────────────

def letter_progress(letter, steps=None):
    """Progress-tracker data for one letter (based on the current routing cycle)."""
    if steps is None:
        steps = get_steps(letter["id"], letter["current_cycle"])
    total = len(steps)
    signed = sum(1 for s in steps if s["status"] == "completed")
    current = next((s for s in steps if s["status"] in ACTIVE_STEP_STATUSES), None)
    status = letter["status"]
    if status == "fully_signed":
        percent = 100
    elif status in ("returned", "disapproved", "expired", "withdrawn"):
        percent, signed = 0, 0  # route restarts from the first office on resubmission
    else:
        percent = int(round(signed * 100.0 / total)) if total else 0
    stopped_at = next((s for s in steps if s["status"] in ("returned", "disapproved")), None)
    for s in steps:
        s["tracker_label"] = STEP_STATUS_LABELS.get(s["status"], ("", s["status"]))[1]
    return {
        "total": total,
        "signed": signed,
        "pending": max(total - signed, 0),
        "percent": percent,
        "current_office": current["office_name"] if current else None,
        "current_step_status": current["status"] if current else None,
        "stopped_at": stopped_at["office_name"] if stopped_at else None,
        "status": status,
        "status_label": status_label(status),
        "last_update": letter["updated_at"],
        "steps": steps,
    }


def letters_for_org(org_id):
    rows = query(
        """SELECT l.*, g.name AS organization_name, u.name AS submitter_name, u.email AS owner_email
             FROM letters l JOIN organizations g ON g.id = l.organization_id
             JOIN users u ON u.id = l.submitted_by
            WHERE l.organization_id = ? AND l.is_withdrawn = 0
            ORDER BY l.updated_at DESC, l.id DESC""",
        (org_id,))
    for r in rows:
        r["progress"] = letter_progress(r)
        last = query("SELECT body, author_name, office_name, kind, created_at FROM letter_comments "
                     "WHERE letter_id = ? ORDER BY id DESC LIMIT 1", (r["id"],), one=True)
        r["last_comment"] = last
        r["decision_comment"] = query(
            "SELECT body, author_name, office_name, kind, created_at FROM letter_comments "
            "WHERE letter_id = ? AND kind IN ('returned','disapproved') ORDER BY id DESC LIMIT 1",
            (r["id"],), one=True)
        r["comment_count"] = query("SELECT COUNT(*) AS n FROM letter_comments WHERE letter_id = ?",
                                   (r["id"],), one=True)["n"]
    return rows


def cancelled_letters_for_org(org_id):
    """Signing requests the organization deleted (withdrawn), newest first."""
    rows = query(
        """SELECT l.*, g.name AS organization_name, u.name AS submitter_name, u.email AS owner_email,
                  a.actor_name AS deleted_by, a.created_at AS deleted_at
             FROM letters l
             JOIN organizations g ON g.id = l.organization_id
             JOIN users u ON u.id = l.submitted_by
             LEFT JOIN activity_log a ON a.id = (SELECT MAX(id) FROM activity_log
                                                  WHERE letter_id = l.id AND action = 'letter_withdrawn')
            WHERE l.organization_id = ? AND l.is_withdrawn = 1
            ORDER BY COALESCE(a.created_at, l.updated_at) DESC, l.id DESC""",
        (org_id,))
    for r in rows:
        steps = get_steps(r["id"], r["current_cycle"])
        r["total_steps"] = len(steps)
        r["signed_before"] = sum(1 for s in steps if s["status"] == "completed")
        # the office that was holding the letter when it was deleted
        held = next((s for s in steps if s["status"] == "cancelled" and s["received_at"]), None)
        r["was_at"] = held["office_name"] if held else None
    return rows


def office_queue(office_id):
    rows = query(
        f"""SELECT s.id AS step_id, s.status AS step_status, s.received_at, s.opened_at, s.step_order,
                   s.endorsed_by, l.*, g.name AS organization_name, e.name AS endorsed_by_name,
                   (SELECT COUNT(*) FROM letter_steps x WHERE x.letter_id = l.id AND x.cycle = l.current_cycle) AS total_steps
              FROM letter_steps s
              JOIN letters l       ON l.id = s.letter_id AND l.current_cycle = s.cycle
              JOIN organizations g ON g.id = l.organization_id
              LEFT JOIN users e    ON e.id = s.endorsed_by
             WHERE s.office_id = ? AND s.status IN ({','.join('?' * len(ACTIVE_STEP_STATUSES))})
               AND l.is_withdrawn = 0 AND l.status IN ({','.join('?' * len(IN_FLIGHT_LETTER_STATUSES))})
             ORDER BY l.submitted_at ASC, s.received_at ASC, l.id ASC""",
        (office_id, *ACTIVE_STEP_STATUSES, *IN_FLIGHT_LETTER_STATUSES))
    for i, r in enumerate(rows, start=1):
        r["queue_no"] = i
        r["queue_status"] = STEP_STATUS_LABELS[r["step_status"]][0]
    return rows


def office_history(office_id, limit=500):
    return query(
        """SELECT s.id AS step_id, s.status AS step_status, s.cycle, s.acted_at, s.received_at,
                  s.step_order, a.name AS acted_by_name, l.id AS letter_id, l.title, l.status AS letter_status,
                  l.tracking_number, l.current_cycle, g.name AS organization_name
             FROM letter_steps s
             JOIN letters l ON l.id = s.letter_id
             JOIN organizations g ON g.id = l.organization_id
             LEFT JOIN users a ON a.id = s.acted_by
            WHERE s.office_id = ? AND s.status IN ('completed','returned','disapproved')
            ORDER BY s.acted_at DESC LIMIT ?""",
        (office_id, limit))


def office_counts(office_id):
    row = query(
        f"""SELECT
              SUM(CASE WHEN s.status = 'queued' THEN 1 ELSE 0 END) AS waiting,
              SUM(CASE WHEN s.status IN ('processing','awaiting_signature') THEN 1 ELSE 0 END) AS processing,
              SUM(CASE WHEN s.status = 'completed' THEN 1 ELSE 0 END) AS completed,
              SUM(CASE WHEN s.status IN ('returned','disapproved') THEN 1 ELSE 0 END) AS returned
            FROM letter_steps s JOIN letters l ON l.id = s.letter_id
           WHERE s.office_id = ? AND l.is_withdrawn = 0
             AND (s.status NOT IN ('queued','processing','awaiting_signature')
                  OR (s.cycle = l.current_cycle AND l.status IN ({','.join('?' * len(IN_FLIGHT_LETTER_STATUSES))})))""",
        (office_id, *IN_FLIGHT_LETTER_STATUSES), one=True)
    return {k: (row[k] or 0) for k in ("waiting", "processing", "completed", "returned")}


def office_directory(q=""):
    """Offices + designated signatories + availability derived from accounts and live workload."""
    rows = query(
        f"""SELECT o.*, u.name AS signatory_name, u.email AS signatory_email, u.position AS signatory_position,
                   u.is_active AS signatory_active, h.step_order AS hierarchy_order,
                   (SELECT COUNT(*) FROM users x WHERE x.office_id = o.id AND x.role = 'office' AND x.is_active = 1) AS active_accounts,
                   (SELECT COUNT(*) FROM letter_steps s JOIN letters l ON l.id = s.letter_id AND l.current_cycle = s.cycle
                     WHERE s.office_id = o.id AND s.status = 'queued' AND l.is_withdrawn = 0
                       AND l.status IN ({','.join('?' * len(IN_FLIGHT_LETTER_STATUSES))})) AS queued_count,
                   (SELECT COUNT(*) FROM letter_steps s JOIN letters l ON l.id = s.letter_id AND l.current_cycle = s.cycle
                     WHERE s.office_id = o.id AND s.status IN ('processing','awaiting_signature') AND l.is_withdrawn = 0
                       AND l.status IN ({','.join('?' * len(IN_FLIGHT_LETTER_STATUSES))})) AS processing_count
              FROM offices o
              LEFT JOIN users u ON u.id = o.signatory_user_id
              LEFT JOIN hierarchy_steps h ON h.office_id = o.id
             ORDER BY CASE WHEN h.step_order IS NULL THEN 1 ELSE 0 END, h.step_order, o.name COLLATE NOCASE""",
        (*IN_FLIGHT_LETTER_STATUSES, *IN_FLIGHT_LETTER_STATUSES))
    q = (q or "").strip().lower()
    labels = dict(AVAILABILITY_CHOICES)
    out = []
    for r in rows:
        accounts = query(
            """SELECT id, name, availability, availability_note, availability_updated_at FROM users
                WHERE office_id = ? AND role = 'office' AND is_active = 1 ORDER BY id""", (r["id"],))
        staff = [s["name"] for s in accounts]
        if q and not (q in (r["name"] or "").lower() or q in (r["signatory_name"] or "").lower()
                      or any(q in n.lower() for n in staff)):
            continue
        # Availability is what the signatory set in their own Admin Portal account.
        # The designated signatory's status counts; otherwise the first active account's.
        source = next((a for a in accounts if a["id"] == r["signatory_user_id"]), None) or \
            next((a for a in accounts if a["availability"] == "available"), None) or \
            (accounts[0] if accounts else None)
        if not r["is_active"] or not source:
            r["availability_key"], r["availability"] = "unavailable", "Unavailable"
            r["availability_note"], r["availability_updated_at"] = "", None
        else:
            key = source["availability"] or "available"
            r["availability_key"] = key
            r["availability"] = labels.get(key, key.replace("_", " ").title())
            r["availability_note"] = source["availability_note"] or ""
            r["availability_updated_at"] = source["availability_updated_at"]
        r["staff"] = staff
        out.append(r)
    return out


def set_availability(user, status, note=""):
    """A signatory updates their own availability (shown to organizations)."""
    if not user or user["role"] != "office":
        raise WorkflowError("Only signatory accounts can set availability.")
    if status not in dict(AVAILABILITY_CHOICES):
        raise WorkflowError("Please choose a valid availability status.")
    note = (note or "").strip()[:200]
    with transaction():
        execute("UPDATE users SET availability = ?, availability_note = ?, availability_updated_at = ? WHERE id = ?",
                (status, note, now_str(), user["id"]))
        log_activity(user, "availability_updated",
                     dict(AVAILABILITY_CHOICES)[status] + (f" – {note}" if note else ""))


def letter_access(user, letter):
    """
    'owner'     – organization that submitted the letter
    'office'    – office that the letter has reached in the CURRENT routing cycle
    'moderator' – management access to metadata/history (not to file contents)
    None        – no access (including offices the letter has not reached yet)
    """
    if not user or not letter:
        return None
    # a deleted (withdrawn) request stays visible only to its organization and moderators
    if letter["is_withdrawn"] and user["role"] not in ("user", "moderator"):
        return None
    if user["role"] == "user":
        return "owner" if letter["organization_id"] == user["organization_id"] else None
    if user["role"] == "moderator":
        return "moderator"
    if user["role"] == "office" and user["office_id"]:
        row = query(
            """SELECT 1 FROM letter_steps WHERE letter_id = ? AND cycle = ? AND office_id = ?
                  AND status NOT IN ('waiting','cancelled')""",
            (letter["id"], letter["current_cycle"], user["office_id"]), one=True)
        if row:
            return "office"
    return None


def system_stats():
    by_status = {r["status"]: r["n"] for r in query(
        "SELECT status, COUNT(*) AS n FROM letters WHERE is_withdrawn = 0 GROUP BY status")}
    total = sum(by_status.values())
    in_flight = sum(by_status.get(s, 0) for s in IN_FLIGHT_LETTER_STATUSES)
    return {
        "total": total,
        "pending": by_status.get("pending", 0),
        "in_progress": in_flight,
        "under_review": by_status.get("under_review", 0) + by_status.get("waiting_signature", 0),
        "processing": by_status.get("processing", 0),
        "fully_signed": by_status.get("fully_signed", 0),
        "disapproved": by_status.get("disapproved", 0),
        "returned": by_status.get("returned", 0),
        "expired": by_status.get("expired", 0),
        "by_status": by_status,
        "users": query("SELECT COUNT(*) AS n FROM users WHERE role = 'user'", one=True)["n"],
        "office_accounts": query("SELECT COUNT(*) AS n FROM users WHERE role = 'office'", one=True)["n"],
        "offices": query("SELECT COUNT(*) AS n FROM offices WHERE is_active = 1", one=True)["n"],
        "tracking_counter": query("SELECT value FROM counters WHERE name = 'tracking'", one=True)["value"],
    }
