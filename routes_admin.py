# routes_admin.py
"""Admin Portal – signatory offices: incoming queue, processing, signing history."""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import workflow
from auth import current_user, render_shell, roles_required
from db import AVAILABILITY_CHOICES
from models import find_office_by_id

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/login", methods=["GET", "POST"])
def login():
    from app import portal_login
    return portal_login("office")


def _queue_ctx(user):
    office = find_office_by_id(user["office_id"])
    queue = workflow.office_queue(user["office_id"])
    return {
        "office": office,
        "queue": queue,
        "counts": workflow.office_counts(user["office_id"]),
        "can_sign": workflow.office_signatory_ok(user, office) if office else False,
        "availability_choices": AVAILABILITY_CHOICES,
    }


@bp.route("/availability", methods=["POST"])
@roles_required("office")
def availability():
    """Signatory updates their own availability (visible to organizations)."""
    user = current_user()
    try:
        workflow.set_availability(user, request.form.get("availability", ""),
                                  request.form.get("availability_note", ""))
        flash("Availability updated.", "success")
    except workflow.WorkflowError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.dashboard"))


@bp.route("/")
@roles_required("office")
def dashboard():
    user = current_user()
    return render_shell(user, "dashboard", "partials/admin_queue.html", **_queue_ctx(user))


@bp.route("/p/queue")
@roles_required("office")
def queue_partial():
    return render_template("partials/admin_queue.html", **_queue_ctx(current_user()))


@bp.route("/p/history")
@roles_required("office")
def history_partial():
    user = current_user()
    return render_template("partials/admin_history.html",
                           office=find_office_by_id(user["office_id"]),
                           history=workflow.office_history(user["office_id"]))
