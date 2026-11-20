# models.py
"""
Data-access helpers on top of db.py.

The previous version kept USERS / DOCUMENTS / SIGN_REQUESTS in Python lists,
which were lost on every restart. Everything now lives in SQLite; the helper
names (find_user_by_email, find_user_by_id, ...) are kept so existing code
reads the same.
"""
from werkzeug.security import check_password_hash, generate_password_hash

from db import DEFAULT_CONTENT, execute, now_str, query, transaction

DEFAULT_CONTENT_KEYS = tuple(DEFAULT_CONTENT)

ROLES = ("user", "office", "moderator")

# Letter (overall) statuses -> display label
LETTER_STATUS_LABELS = {
    "pending": "Pending",
    # every in-flight state is shown simply as "Processing"
    "under_review": "Processing",
    "waiting_signature": "Processing",
    "processing": "Processing",
    "approved": "Approved",
    "fully_signed": "Fully Signed",
    "disapproved": "Disapproved",
    "returned": "Returned for Revision",
    "expired": "Expired",
    "withdrawn": "Cancelled",
}

# Per-office step statuses -> (queue label, tracker label)
STEP_STATUS_LABELS = {
    "waiting": ("Not Yet Received", "Waiting"),
    "queued": ("Waiting in Queue", "Pending"),
    "processing": ("Processing", "Processing"),
    "awaiting_signature": ("Processing", "Processing"),
    "completed": ("Completed", "Approved & Signed"),
    "returned": ("Returned for Revision", "Returned for Revision"),
    "disapproved": ("Disapproved", "Disapproved"),
    "cancelled": ("Cancelled", "Not Reached"),
}

ACTIVE_STEP_STATUSES = ("queued", "processing", "awaiting_signature")
IN_FLIGHT_LETTER_STATUSES = ("pending", "under_review", "waiting_signature", "processing")
REVISABLE_LETTER_STATUSES = ("returned", "disapproved", "expired")

PRIORITY_VALUE = {"high": 1, "medium": 2, "low": 3}


# ───────────── Users ─────────────

def hash_password(raw):
    return generate_password_hash(raw)


def verify_password(user, raw):
    if not user or not user.get("password_hash"):
        return False
    return check_password_hash(user["password_hash"], raw)


def find_user_by_email(email):
    if not email:
        return None
    return query("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email.strip(),), one=True)


def find_user_by_id(user_id):
    if not user_id:
        return None
    return query(
        """SELECT u.*, o.name AS office_name, g.name AS organization_name
             FROM users u
             LEFT JOIN offices o       ON o.id = u.office_id
             LEFT JOIN organizations g ON g.id = u.organization_id
            WHERE u.id = ?""",
        (user_id,),
        one=True,
    )


def create_user(email, password, name, role, organization_id=None, office_id=None,
                position="", is_active=1):
    if role not in ROLES:
        raise ValueError("invalid role")
    with transaction():
        return execute(
            """INSERT INTO users(email, password_hash, name, role, organization_id, office_id,
                                 position, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (email.strip().lower(), hash_password(password), name.strip(), role,
             organization_id, office_id, position.strip(), int(is_active), now_str()),
        )


def user_ids_for_organization(org_id):
    return [r["id"] for r in query(
        "SELECT id FROM users WHERE organization_id = ? AND role = 'user' AND is_active = 1", (org_id,))]


def user_ids_for_office(office_id):
    return [r["id"] for r in query(
        "SELECT id FROM users WHERE office_id = ? AND role = 'office' AND is_active = 1", (office_id,))]


def moderator_ids():
    return [r["id"] for r in query("SELECT id FROM users WHERE role = 'moderator' AND is_active = 1")]


# ───────────── Organizations ─────────────

def find_org_by_name(name):
    return query("SELECT * FROM organizations WHERE name = ? COLLATE NOCASE", (name.strip(),), one=True)


def find_org_by_id(org_id):
    return query("SELECT * FROM organizations WHERE id = ?", (org_id,), one=True)


def create_org(name, contact_email=""):
    with transaction():
        return execute(
            "INSERT INTO organizations(name, contact_email, created_at) VALUES (?, ?, ?)",
            (name.strip(), contact_email, now_str()),
        )


# ───────────── Offices ─────────────

def find_office_by_id(office_id):
    return query(
        """SELECT o.*, u.name AS signatory_name, u.email AS signatory_email,
                  u.position AS signatory_position, u.is_active AS signatory_active
             FROM offices o LEFT JOIN users u ON u.id = o.signatory_user_id
            WHERE o.id = ?""",
        (office_id,),
        one=True,
    )


def list_offices(active_only=False):
    sql = """SELECT o.*, u.name AS signatory_name, u.email AS signatory_email,
                    u.position AS signatory_position, u.is_active AS signatory_active,
                    h.step_order AS hierarchy_order
               FROM offices o
               LEFT JOIN users u ON u.id = o.signatory_user_id
               LEFT JOIN hierarchy_steps h ON h.office_id = o.id"""
    if active_only:
        sql += " WHERE o.is_active = 1"
    sql += " ORDER BY o.name COLLATE NOCASE"
    return query(sql)


def get_hierarchy(active_only=False):
    """Current signing hierarchy (ordered)."""
    sql = """SELECT h.step_order, o.*, u.name AS signatory_name
               FROM hierarchy_steps h
               JOIN offices o ON o.id = h.office_id
               LEFT JOIN users u ON u.id = o.signatory_user_id"""
    if active_only:
        sql += " WHERE o.is_active = 1"
    sql += " ORDER BY h.step_order"
    return query(sql)


# ───────────── Site content ─────────────

def get_content(key, default=""):
    row = query("SELECT value FROM site_content WHERE key = ?", (key,), one=True)
    return row["value"] if row else default


def all_content():
    return {r["key"]: r["value"] for r in query("SELECT key, value FROM site_content")}


def parse_contact_rows(raw):
    rows = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        if "|" in line:
            a, b = line.split("|", 1)
            rows.append((a.strip(), b.strip()))
        else:
            rows.append((line.strip(), ""))
    return rows


# ───────────── Letters ─────────────

def find_letter_by_id(letter_id):
    return query(
        """SELECT l.*, g.name AS organization_name, u.name AS submitter_name, u.email AS owner_email
             FROM letters l
             JOIN organizations g ON g.id = l.organization_id
             JOIN users u ON u.id = l.submitted_by
            WHERE l.id = ?""",
        (letter_id,),
        one=True,
    )


def get_steps(letter_id, cycle):
    return query(
        """SELECT s.*, a.name AS acted_by_name, e.name AS endorsed_by_name
             FROM letter_steps s
             LEFT JOIN users a ON a.id = s.acted_by
             LEFT JOIN users e ON e.id = s.endorsed_by
            WHERE s.letter_id = ? AND s.cycle = ?
            ORDER BY s.step_order""",
        (letter_id, cycle),
    )


def get_current_step(letter_id, cycle):
    """The step currently holding the letter (first not-completed step of the cycle)."""
    return query(
        """SELECT * FROM letter_steps
            WHERE letter_id = ? AND cycle = ? AND status IN ('queued','processing','awaiting_signature')
            ORDER BY step_order LIMIT 1""",
        (letter_id, cycle),
        one=True,
    )


def get_versions(letter_id):
    return query(
        """SELECT v.*, u.name AS uploaded_by_name FROM letter_versions v
             LEFT JOIN users u ON u.id = v.uploaded_by
            WHERE v.letter_id = ? ORDER BY v.version_no DESC""",
        (letter_id,),
    )


def get_version(version_id):
    return query("SELECT * FROM letter_versions WHERE id = ?", (version_id,), one=True)


def get_comments(letter_id):
    return query(
        "SELECT * FROM letter_comments WHERE letter_id = ? ORDER BY created_at DESC, id DESC",
        (letter_id,),
    )


def get_signatures(letter_id, cycle):
    return query(
        "SELECT * FROM letter_signatures WHERE letter_id = ? AND cycle = ? ORDER BY id",
        (letter_id, cycle),
    )


def get_letter_activity(letter_id):
    return query(
        "SELECT * FROM activity_log WHERE letter_id = ? ORDER BY id DESC",
        (letter_id,),
    )
