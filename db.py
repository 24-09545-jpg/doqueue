# db.py
"""
SQLite persistence layer for the DSA System.

Replaces the old in-memory lists (USERS / DOCUMENTS / SIGN_REQUESTS) so that
letters, signatures, comments, queues, notifications and the audit trail
survive restarts and can be enforced by the backend.

Rows are returned as `Row` objects (a dict that also allows attribute access),
so Jinja templates can keep using `doc.title`, `user.email`, etc.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DSA_DB_PATH", os.path.join(BASE_DIR, "dsa.sqlite3"))


class Row(dict):
    """dict with attribute access (missing keys -> None)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _row_factory(cursor, row):
    return Row((col[0], row[idx]) for idx, col in enumerate(cursor.description))


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_local_conn = {}


def connect(path=None):
    path = path or DB_PATH
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = _row_factory
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_db():
    """Connection for the current Flask request (or a module-level one outside requests)."""
    try:
        from flask import g, has_app_context
        if has_app_context():
            if "db" not in g:
                g.db = connect()
            return g.db
    except ImportError:  # pragma: no cover
        pass
    key = DB_PATH
    if key not in _local_conn:
        _local_conn[key] = connect()
    return _local_conn[key]


def close_db(_exc=None):
    try:
        from flask import g
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()
    except Exception:
        pass


def reset_module_connections():
    for c in _local_conn.values():
        try:
            c.close()
        except Exception:
            pass
    _local_conn.clear()


@contextmanager
def transaction():
    """
    Write transaction. BEGIN IMMEDIATE takes the write lock up front, so two
    signatories finishing letters at the same moment are serialized — this is
    what keeps tracking numbers unique and queue state consistent.
    Nested use joins the outer transaction.
    """
    conn = get_db()
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def query(sql, params=(), one=False):
    cur = get_db().execute(sql, params)
    rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, params=()):
    cur = get_db().execute(sql, params)
    return cur.lastrowid


SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description   TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offices (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description       TEXT NOT NULL DEFAULT '',
    contact_email     TEXT NOT NULL DEFAULT '',
    contact_phone     TEXT NOT NULL DEFAULT '',
    location          TEXT NOT NULL DEFAULT '',
    signatory_user_id INTEGER,
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    email              TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash      TEXT NOT NULL,
    name               TEXT NOT NULL,
    role               TEXT NOT NULL CHECK (role IN ('user', 'office', 'moderator')),
    organization_id    INTEGER REFERENCES organizations(id),
    office_id          INTEGER REFERENCES offices(id),
    position           TEXT NOT NULL DEFAULT '',
    is_active          INTEGER NOT NULL DEFAULT 1,
    signature_path     TEXT,
    signature_filename TEXT,
    created_at         TEXT NOT NULL,
    last_login_at      TEXT
);

-- Current signing hierarchy (template for NEW submissions only).
CREATE TABLE IF NOT EXISTS hierarchy_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    step_order INTEGER NOT NULL,
    office_id  INTEGER NOT NULL UNIQUE REFERENCES offices(id)
);

CREATE TABLE IF NOT EXISTS letters (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id    INTEGER NOT NULL REFERENCES organizations(id),
    submitted_by       INTEGER NOT NULL REFERENCES users(id),
    title              TEXT NOT NULL,
    note               TEXT NOT NULL DEFAULT '',
    priority           TEXT NOT NULL DEFAULT 'low',
    expiry_date        TEXT,
    status             TEXT NOT NULL DEFAULT 'pending',
    current_cycle      INTEGER NOT NULL DEFAULT 1,
    current_version_id INTEGER,
    working_file_path  TEXT,
    final_file_path    TEXT,
    tracking_seq       INTEGER UNIQUE,
    tracking_number    TEXT UNIQUE,
    is_withdrawn       INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    submitted_at       TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    completed_at       TEXT
);

CREATE TABLE IF NOT EXISTS letter_versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    letter_id         INTEGER NOT NULL REFERENCES letters(id),
    version_no        INTEGER NOT NULL,
    file_path         TEXT NOT NULL,
    original_filename TEXT NOT NULL DEFAULT '',
    uploaded_by       INTEGER REFERENCES users(id),
    remarks           TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    UNIQUE (letter_id, version_no)
);

-- One row per office per routing cycle. A new cycle starts on every resubmission;
-- old cycles are kept as history.
CREATE TABLE IF NOT EXISTS letter_steps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    letter_id    INTEGER NOT NULL REFERENCES letters(id),
    cycle        INTEGER NOT NULL,
    version_id   INTEGER REFERENCES letter_versions(id),
    step_order   INTEGER NOT NULL,
    office_id    INTEGER NOT NULL REFERENCES offices(id),
    office_name  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'waiting',
    received_at  TEXT,
    opened_at    TEXT,
    endorsed_by  INTEGER REFERENCES users(id),
    endorsed_at  TEXT,
    acted_by     INTEGER REFERENCES users(id),
    acted_at     TEXT,
    UNIQUE (letter_id, cycle, step_order)
);

CREATE TABLE IF NOT EXISTS letter_signatures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    letter_id   INTEGER NOT NULL REFERENCES letters(id),
    step_id     INTEGER NOT NULL REFERENCES letter_steps(id),
    cycle       INTEGER NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    office_id   INTEGER NOT NULL REFERENCES offices(id),
    signer_name TEXT NOT NULL,
    office_name TEXT NOT NULL,
    image_path  TEXT NOT NULL,
    page_index  INTEGER NOT NULL DEFAULT 0,
    rel_x       REAL NOT NULL,
    rel_y       REAL NOT NULL,
    rel_w       REAL NOT NULL,
    rel_h       REAL NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS letter_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    letter_id   INTEGER NOT NULL REFERENCES letters(id),
    cycle       INTEGER NOT NULL,
    step_id     INTEGER REFERENCES letter_steps(id),
    user_id     INTEGER REFERENCES users(id),
    office_id   INTEGER REFERENCES offices(id),
    author_name TEXT NOT NULL,
    office_name TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'comment',
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    letter_id  INTEGER REFERENCES letters(id),
    kind       TEXT NOT NULL,
    message    TEXT NOT NULL,
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    actor_name    TEXT NOT NULL DEFAULT 'System',
    actor_role    TEXT NOT NULL DEFAULT 'system',
    office_id     INTEGER,
    letter_id     INTEGER,
    action        TEXT NOT NULL,
    details       TEXT NOT NULL DEFAULT '',
    ip            TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_content (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT,
    updated_by INTEGER
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS self_sign_docs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL REFERENCES users(id),
    title         TEXT NOT NULL,
    signed_path   TEXT NOT NULL,
    original_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_steps_office_status ON letter_steps(office_id, status);
CREATE INDEX IF NOT EXISTS idx_steps_letter_cycle  ON letter_steps(letter_id, cycle);
CREATE INDEX IF NOT EXISTS idx_letters_org         ON letters(organization_id);
CREATE INDEX IF NOT EXISTS idx_notif_user          ON notifications(user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_activity_letter     ON activity_log(letter_id);
CREATE INDEX IF NOT EXISTS idx_comments_letter     ON letter_comments(letter_id);
"""

DEFAULT_CONTENT = {
    "announcement": "",
    "submission_instructions": (
        "Upload your organization's event letter as a PDF. It will be routed "
        "automatically through the required signatory offices in order."
    ),
    "login_notice": "",
    "contacts_title": "ICT Services Email Addresses",
    "contacts_intro": "For any portal login concerns contact us through the following email addresses:",
    "contacts_rows": "\n".join([
        "Pablo Borbon | ict.pb@g.batstate-u.edu.ph",
        "Alangilan | ict.alangilan@g.batstate-u.edu.ph",
        "Malvar | ict.malvar@g.batstate-u.edu.ph",
        "Nasugbu | ictsupport.nasugbu@g.batstate-u.edu.ph",
        "Lipa | ict.lipa@g.batstate-u.edu.ph",
        "Balayan | ict.balayan@g.batstate-u.edu.ph",
        "Lemery | ict.lemery@g.batstate-u.edu.ph",
        "San Juan | ict.sanjuan@g.batstate-u.edu.ph",
        "Rosario | ict.rosario@g.batstate-u.edu.ph",
        "Lobo | ict.lobo@g.batstate-u.edu.ph",
    ]),
}


OFFICIAL_HIERARCHY = [
    "Organization Adviser",
    "Student Coordinator",
    "Office of Student Organization",
    "Program Chairperson",
    "Department Chairperson",
    "College Dean",
    "Vice Chancellor for Academic Affairs",
    "Vice Chancellor for Administration and Finance",
]

# Availability a signatory can set for themself (key -> label)
AVAILABILITY_CHOICES = [
    ("available", "Available"),
    ("busy", "Busy"),
    ("in_meeting", "In a Meeting"),
    ("on_leave", "On Leave"),
    ("out_of_office", "Out of Office"),
]


def _migrate(conn):
    """Add columns introduced after the first release (safe to run repeatedly)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    for name, ddl in (
        ("availability", "TEXT NOT NULL DEFAULT 'available'"),
        ("availability_note", "TEXT NOT NULL DEFAULT ''"),
        ("availability_updated_at", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")


def _apply_official_hierarchy():
    """
    One-time setup of the official signing route. Creates any missing offices and
    sets the hierarchy to the official order. Runs once per database (flag in
    counters), so later changes made by moderators are never overwritten.
    Letters already in progress keep their own route.
    """
    if query("SELECT 1 FROM counters WHERE name = 'official_hierarchy_v1'", one=True):
        return
    with transaction():
        ids = []
        for name in OFFICIAL_HIERARCHY:
            row = query("SELECT id FROM offices WHERE name = ? COLLATE NOCASE", (name,), one=True)
            if row:
                execute("UPDATE offices SET is_active = 1 WHERE id = ?", (row["id"],))
                ids.append(row["id"])
            else:
                ids.append(execute(
                    "INSERT INTO offices(name, created_at, updated_at) VALUES (?, ?, ?)",
                    (name, now_str(), now_str())))
        execute("DELETE FROM hierarchy_steps")
        for order, oid in enumerate(ids, start=1):
            execute("INSERT INTO hierarchy_steps(step_order, office_id) VALUES (?, ?)", (order, oid))
        execute("INSERT INTO counters(name, value) VALUES ('official_hierarchy_v1', 1)")
        execute(
            """INSERT INTO activity_log(actor_name, actor_role, action, details, created_at)
               VALUES ('System', 'system', 'hierarchy_updated', ?, ?)""",
            ("Official signing route applied: " + " → ".join(OFFICIAL_HIERARCHY), now_str()))


def init_db(seed_moderator=True):
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate(conn)
    _apply_official_hierarchy()
    with transaction():
        conn.execute("INSERT OR IGNORE INTO counters(name, value) VALUES ('tracking', 0)")
        for k, v in DEFAULT_CONTENT.items():
            conn.execute(
                "INSERT OR IGNORE INTO site_content(key, value, updated_at) VALUES (?, ?, ?)",
                (k, v, now_str()),
            )
    if seed_moderator:
        _seed_default_moderator()


def _seed_default_moderator():
    """Create the first moderator account if none exists."""
    from werkzeug.security import generate_password_hash

    existing = query("SELECT id FROM users WHERE role = 'moderator' LIMIT 1", one=True)
    if existing:
        return None
    email = os.environ.get("DSA_MODERATOR_EMAIL", "moderator@g.batstate-u.edu.ph")
    password = os.environ.get("DSA_MODERATOR_PASSWORD", "ChangeMe123!")
    with transaction():
        execute(
            "INSERT INTO users(email, password_hash, name, role, created_at) VALUES (?, ?, ?, 'moderator', ?)",
            (email, generate_password_hash(password), "DSA Moderator", now_str()),
        )
    print(f"[DSA] Created default moderator account: {email} / {password}  (change this password!)")
    return email
