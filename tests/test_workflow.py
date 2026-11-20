"""
End-to-end test of the DSA workflow through the real Flask routes:
three portals, sequential routing, access control, return/resubmit,
signature placement and tracking-number generation.
"""
import importlib
import io
import re
import sys
from datetime import date, timedelta

import pytest
from PIL import Image
from PyPDF2 import PdfReader
from reportlab.pdfgen import canvas

DOMAIN = "@g.batstate-u.edu.ph"
FUTURE = (date.today() + timedelta(days=30)).isoformat()


def make_pdf(text="Event letter", pages=2):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    for i in range(pages):
        c.drawString(72, 720, f"{text} – page {i + 1}")
        c.showPage()
    c.save()
    buf.seek(0)
    return buf


def make_sig():
    img = Image.new("RGB", (300, 100), "white")
    for x in range(30, 270):
        for y in (48, 49, 50, 51):
            img.putpixel((x, y), (10, 10, 60))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dsa")
    mp = pytest.MonkeyPatch()
    mp.setenv("DSA_DB_PATH", str(tmp / "dsa.sqlite3"))
    mp.setenv("DSA_UPLOAD_FOLDER", str(tmp / "uploads"))
    mp.setenv("DSA_MODERATOR_EMAIL", "mod" + DOMAIN)
    mp.setenv("DSA_MODERATOR_PASSWORD", "modpass123")
    import db
    db.reset_module_connections()
    mp.setattr(db, "DB_PATH", str(tmp / "dsa.sqlite3"))
    if "app" in sys.modules:
        app_mod = importlib.reload(sys.modules["app"])
    else:
        app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    yield app_mod
    db.reset_module_connections()
    mp.undo()


class Client:
    def __init__(self, app_mod):
        self.c = app_mod.app.test_client()

    def token(self):
        self.c.get("/login")
        with self.c.session_transaction() as s:
            return s.get("_csrf")

    def post(self, url, data=None, ajax=False, **kw):
        data = dict(data or {})
        data["csrf_token"] = self.token()
        headers = {"X-Requested-With": "XMLHttpRequest"} if ajax else {}
        return self.c.post(url, data=data, headers=headers, content_type="multipart/form-data", **kw)

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def login(self, portal_url, email, password):
        return self.post(portal_url, {"email": email, "password": password})


def route():
    return [str(r['office_id']) for r in q('SELECT office_id FROM hierarchy_steps ORDER BY step_order')]


def q(sql, params=()):
    import db
    return db.query(sql, params)


def test_full_workflow(env):
    mod = Client(env)
    r = mod.login("/moderator/login", "mod" + DOMAIN, "modpass123")
    assert r.status_code == 302 and "/moderator" in r.headers["Location"]

    # ── moderator sets up offices, accounts and hierarchy
    for name in ("Office A", "Office B"):
        assert mod.post("/moderator/offices/save", {"name": name}).status_code == 302
    a_id = q("SELECT id FROM offices WHERE name = 'Office A'")[0]["id"]
    b_id = q("SELECT id FROM offices WHERE name = 'Office B'")[0]["id"]
    mod.post("/moderator/accounts/save", {"name": "Sig A", "email": "siga" + DOMAIN, "role": "office",
                                          "password": "password1", "office_id": a_id, "make_signatory": "1"})
    mod.post("/moderator/accounts/save", {"name": "Staff A", "email": "staffa" + DOMAIN, "role": "office",
                                          "password": "password1", "office_id": a_id})
    mod.post("/moderator/accounts/save", {"name": "Sig B", "email": "sigb" + DOMAIN, "role": "office",
                                          "password": "password1", "office_id": b_id, "make_signatory": "1"})
    r = mod.c.post("/moderator/hierarchy/save", data={"office_ids": [str(a_id), str(b_id)],
                                                      "csrf_token": mod.token()})
    assert r.status_code == 302
    assert [h["office_id"] for h in q("SELECT office_id FROM hierarchy_steps ORDER BY step_order")] == [a_id, b_id]

    # ── organization registers & submits
    org = Client(env)
    r = org.post("/register", {"email": "org" + DOMAIN, "name": "Org User", "organization": "CpE Org",
                               "password": "password1"})
    assert r.status_code == 302
    r = org.post("/upload-document", {"title": "Event Letter", "expiry_date": FUTURE, "note": "pls", "office_ids": route(),
                                      "document": (make_pdf(), "letter.pdf")})
    assert r.status_code == 302, r.data[:500]
    letter = q("SELECT * FROM letters")[0]
    lid = letter["id"]
    assert letter["status"] == "pending"

    # a second person joining the same org needs approval (cannot read its letters right away)
    other = Client(env)
    other.post("/register", {"email": "intruder" + DOMAIN, "name": "X", "organization": "cpe org",
                             "password": "password1"})
    assert other.get("/dashboard").status_code == 302  # not logged in / inactive

    # ── wrong portal is rejected
    bad = Client(env)
    r = bad.login("/login", "siga" + DOMAIN, "password1")
    assert r.status_code == 200 and b"cannot sign in" in r.data

    sig_a, staff_a, sig_b = Client(env), Client(env), Client(env)
    sig_a.login("/admin/login", "siga" + DOMAIN, "password1")
    staff_a.login("/admin/login", "staffa" + DOMAIN, "password1")
    sig_b.login("/admin/login", "sigb" + DOMAIN, "password1")

    # ── Office B cannot see the letter yet, not even by direct URL
    assert sig_b.get(f"/p/document/{lid}").status_code == 404
    assert sig_b.get(f"/letters/{lid}/file").status_code == 404
    assert b"Event Letter" not in sig_b.get("/admin/p/queue").data
    assert b"Event Letter" in sig_a.get("/admin/p/queue").data

    # moderators monitor but cannot open file contents
    assert mod.get(f"/p/document/{lid}").status_code == 200
    assert mod.get(f"/letters/{lid}/file").status_code == 404

    # ── staff opens & endorses; staff cannot sign
    assert staff_a.get(f"/p/document/{lid}").status_code == 200
    assert q("SELECT status FROM letters")[0]["status"] == "under_review"
    staff_a.post("/upload-signature", {"signature": (make_sig(), "s.png"), "remove_background": "1"}, ajax=True)
    r = staff_a.post(f"/document/{lid}/action", {"action": "approve_sign", "page_index": "0",
                                                 "rel_x": "0.5", "rel_y": "0.7", "rel_w": "0.2"}, ajax=True)
    assert r.status_code == 400 and b"designated signatory" in r.data
    r = staff_a.post(f"/document/{lid}/action", {"action": "endorse", "remarks": "Looks good"}, ajax=True)
    assert r.json["ok"]
    assert q("SELECT status FROM letters")[0]["status"] == "waiting_signature"

    # ── CSRF required
    r = sig_a.c.post(f"/document/{lid}/action", data={"action": "endorse"},
                     headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 400

    # ── signatory A signs
    r = sig_a.post(f"/document/{lid}/action", {"action": "approve_sign", "page_index": "0",
                                               "rel_x": "0.5", "rel_y": "0.7", "rel_w": "0.2"}, ajax=True)
    assert r.status_code == 400 and b"upload a signature" in r.data
    r = sig_a.post("/upload-signature", {"signature": (make_sig(), "s.png"), "remove_background": "1"}, ajax=True)
    assert r.json["ok"]
    r = sig_a.post(f"/document/{lid}/action", {"action": "approve_sign", "page_index": "0",
                                               "rel_x": "0.5", "rel_y": "0.7", "rel_w": "0.2"}, ajax=True)
    assert r.json["ok"], r.data
    letter = q("SELECT * FROM letters")[0]
    assert letter["status"] == "processing"
    import workflow
    assert workflow.letter_progress(letter)["percent"] == 50

    # B now has it; A can no longer act
    assert b"Event Letter" in sig_b.get("/admin/p/queue").data
    r = sig_a.post(f"/document/{lid}/action", {"action": "return", "remarks": "x" * 5}, ajax=True)
    assert r.status_code == 400

    # ── B returns for revision -> progress reset, history kept
    r = sig_b.post(f"/document/{lid}/action", {"action": "return", "remarks": ""}, ajax=True)
    assert r.status_code == 400  # reason required
    r = sig_b.post(f"/document/{lid}/action", {"action": "return", "remarks": "Fix the date"}, ajax=True)
    assert r.json["ok"]
    letter = q("SELECT * FROM letters")[0]
    assert letter["status"] == "returned"
    assert workflow.letter_progress(letter)["percent"] == 0
    page = org.get(f"/p/document/{lid}").data
    assert b"Fix the date" in page and b"Office B" in page

    # ── org resubmits a revised file -> restarts at Office A; B loses access
    r = org.post(f"/letters/{lid}/resubmit", {"title": "Event Letter v2", "expiry_date": FUTURE,
                                              "remarks": "Date fixed",
                                              "document": (make_pdf("revised"), "v2.pdf")}, ajax=True)
    assert r.json["ok"], r.data
    letter = q("SELECT * FROM letters")[0]
    assert letter["current_cycle"] == 2 and letter["status"] == "pending"
    assert len(q("SELECT * FROM letter_versions WHERE letter_id = ?", (lid,))) == 2
    assert len(q("SELECT * FROM letter_steps WHERE letter_id = ?", (lid,))) == 4  # history kept
    assert sig_b.get(f"/p/document/{lid}").status_code == 404

    for c in (sig_a, sig_b):
        if c is sig_b:
            c.post("/upload-signature", {"signature": (make_sig(), "s.png"), "remove_background": "1"}, ajax=True)
        r = c.post(f"/document/{lid}/action", {"action": "approve_sign", "page_index": "-1",
                                               "rel_x": "0.1", "rel_y": "0.8", "rel_w": "0.25"}, ajax=True)
        assert r.json["ok"], r.data

    letter = q("SELECT * FROM letters")[0]
    year = date.today().year
    assert letter["status"] == "fully_signed"
    assert letter["tracking_number"] == f"DSA-{year}-0001"

    # ── final download: stable tracking number, stamped in the PDF
    r1 = org.get(f"/document/{lid}/signed-download")
    r2 = org.get(f"/document/{lid}/signed-download")
    assert r1.status_code == 200 and r1.data == r2.data
    text = PdfReader(io.BytesIO(r1.data)).pages[1].extract_text()
    assert f"DSA-{year}-0001" in text
    assert q("SELECT value FROM counters WHERE name = 'tracking'")[0]["value"] == 1

    # ── second letter gets the next number
    org.post("/upload-document", {"title": "Second", "expiry_date": FUTURE, "office_ids": route(), "document": (make_pdf(), "b.pdf")})
    lid2 = q("SELECT id FROM letters WHERE title = 'Second'")[0]["id"]
    for c in (sig_a, sig_b):
        r = c.post(f"/document/{lid2}/action", {"action": "approve_sign", "page_index": "0",
                                                "rel_x": "0.6", "rel_y": "0.75", "rel_w": "0.2"}, ajax=True)
        assert r.json["ok"], r.data
    assert q("SELECT tracking_number FROM letters WHERE id = ?", (lid2,))[0]["tracking_number"] == f"DSA-{year}-0002"

    # ── disapprove path on a third letter
    org.post("/upload-document", {"title": "Third", "expiry_date": FUTURE, "office_ids": route(), "document": (make_pdf(), "c.pdf")})
    lid3 = q("SELECT id FROM letters WHERE title = 'Third'")[0]["id"]
    r = sig_a.post(f"/document/{lid3}/action", {"action": "disapprove", "remarks": "Not allowed"}, ajax=True)
    assert r.json["ok"]
    assert q("SELECT status, tracking_number FROM letters WHERE id = ?", (lid3,))[0]["tracking_number"] is None

    # ── dashboards / portal pages render
    for c, urls in (
        (org, ["/dashboard", "/p/dashboard", "/p/notifications", "/p/settings", "/p/contacts",
               "/p/sign-yourself", "/p/upload-document", "/api/directory?q=sig"]),
        (sig_a, ["/admin/", "/admin/p/queue", "/admin/p/history", "/p/notifications"]),
        (mod, ["/moderator/", "/moderator/p/overview", "/moderator/p/letters?status=in_progress",
               "/moderator/p/offices", "/moderator/p/accounts", "/moderator/p/hierarchy",
               "/moderator/p/content", "/moderator/p/activity?q=signed"]),
    ):
        for u in urls:
            r = c.get(u)
            assert r.status_code == 200, (u, r.status_code)

    # cross-portal access is blocked
    assert org.get("/moderator/p/offices").status_code in (302, 403)
    assert sig_a.get("/p/dashboard").status_code in (302, 403)

    # notifications were generated for the organization
    assert q("SELECT COUNT(*) AS n FROM notifications n JOIN users u ON u.id = n.user_id "
             "WHERE u.email = ?", ("org" + DOMAIN,))[0]["n"] >= 5
    # audit trail
    actions = {r["action"] for r in q("SELECT action FROM activity_log")}
    assert {"letter_submitted", "letter_signed", "letter_returned", "letter_resubmitted",
            "letter_fully_signed", "hierarchy_updated"} <= actions

    # directory availability reflects workload / accounts
    d = {o["name"]: o["availability"] for o in workflow.office_directory()}
    assert d["Office A"] == "Available"


def test_official_hierarchy_seeded_on_new_database(tmp_path, monkeypatch):
    import db
    from db import OFFICIAL_HIERARCHY
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "fresh.sqlite3"))
    db.reset_module_connections()
    try:
        db.init_db(seed_moderator=False)
        db.init_db(seed_moderator=False)  # idempotent
        names = [r["name"] for r in db.query(
            "SELECT o.name FROM hierarchy_steps h JOIN offices o ON o.id = h.office_id ORDER BY h.step_order")]
        assert names == OFFICIAL_HIERARCHY
    finally:
        db.reset_module_connections()


def test_signatory_availability_visible_only_to_organizations(env):
    sig_b = Client(env)
    sig_b.login("/admin/login", "sigb" + DOMAIN, "password1")
    page = sig_b.get("/admin/p/queue").data
    assert b"My Availability" in page
    assert b"directory-table" not in page and b"Workload" not in page  # no directory/workload on the admin side
    assert sig_b.get("/api/directory").status_code in (302, 403)

    r = sig_b.post("/admin/availability", {"availability": "on_leave", "availability_note": "Back on Monday"})
    assert r.status_code == 302
    r = sig_b.post("/admin/availability", {"availability": "bogus"})
    assert q("SELECT availability FROM users WHERE email = ?", ("sigb" + DOMAIN,))[0]["availability"] == "on_leave"

    org = Client(env)
    org.login("/login", "org" + DOMAIN, "password1")
    rows = org.get("/api/directory?q=office b").get_data(as_text=True)
    assert "On Leave" in rows and "Back on Monday" in rows
    dash = org.get("/p/dashboard").get_data(as_text=True)
    assert "Availability is based on the office's active accounts and its current queue." in dash


def test_organization_chooses_signatories(env):
    org = Client(env)
    org.login("/login", "org" + DOMAIN, "password1")
    a_id, b_id = route()

    # nothing chosen -> rejected, nothing saved
    before = len(q("SELECT id FROM letters"))
    org.post("/upload-document", {"title": "None chosen", "expiry_date": FUTURE, "document": (make_pdf(), "n.pdf")})
    assert len(q("SELECT id FROM letters")) == before

    # an office outside the hierarchy -> rejected
    org.post("/upload-document", {"title": "Bad", "expiry_date": FUTURE, "office_ids": ["99999"],
                                  "document": (make_pdf(), "x.pdf")})
    assert not q("SELECT id FROM letters WHERE title = 'Bad'")

    # only Office B chosen -> goes straight to B, Office A never sees it
    org.post("/upload-document", {"title": "Only B", "expiry_date": FUTURE, "office_ids": [b_id],
                                  "document": (make_pdf(), "o.pdf")})
    lid = q("SELECT id FROM letters WHERE title = 'Only B'")[0]["id"]
    steps = q("SELECT office_id, status FROM letter_steps WHERE letter_id = ?", (lid,))
    assert [(s["office_id"], s["status"]) for s in steps] == [(int(b_id), "queued")]
    sig_a, sig_b = Client(env), Client(env)
    sig_a.login("/admin/login", "siga" + DOMAIN, "password1")
    sig_b.login("/admin/login", "sigb" + DOMAIN, "password1")
    assert sig_a.get(f"/p/document/{lid}").status_code == 404

    # B returns it; the resubmission keeps the same single signatory
    sig_b.post(f"/document/{lid}/action", {"action": "return", "remarks": "Add the venue"}, ajax=True)
    r = org.post(f"/letters/{lid}/resubmit", {"title": "Only B", "expiry_date": FUTURE, "remarks": "added"}, ajax=True)
    assert r.json["ok"], r.data
    steps = q("SELECT office_id FROM letter_steps WHERE letter_id = ? AND cycle = 2", (lid,))
    assert [s["office_id"] for s in steps] == [int(b_id)]

    # B signs -> fully signed with just one signature
    r = sig_b.post(f"/document/{lid}/action", {"action": "approve_sign", "page_index": "0",
                                               "rel_x": "0.5", "rel_y": "0.7", "rel_w": "0.2"}, ajax=True)
    assert r.json["ok"], r.data
    assert q("SELECT status FROM letters WHERE id = ?", (lid,))[0]["status"] == "fully_signed"


def test_organization_deletes_request(env):
    org = Client(env)
    org.login("/login", "org" + DOMAIN, "password1")
    org.post("/upload-document", {"title": "To delete", "expiry_date": FUTURE, "office_ids": route(),
                                  "document": (make_pdf(), "del.pdf")})
    lid = q("SELECT id FROM letters WHERE title = 'To delete'")[0]["id"]
    sig_a = Client(env)
    sig_a.login("/admin/login", "siga" + DOMAIN, "password1")
    assert b"To delete" in sig_a.get("/admin/p/queue").data
    assert b"Delete Request" in org.get(f"/p/document/{lid}").data

    # another organization's account cannot delete it
    other = Client(env)
    other.post("/register", {"email": "other" + DOMAIN, "name": "O", "organization": "Other Org",
                             "password": "password1"})
    r = other.post(f"/document/{lid}/delete", ajax=True)
    assert r.status_code == 400
    assert q("SELECT is_withdrawn FROM letters WHERE id = ?", (lid,))[0]["is_withdrawn"] == 0

    r = org.post(f"/document/{lid}/delete", ajax=True)
    assert r.json["ok"]
    assert b"To delete" not in sig_a.get("/admin/p/queue").data      # gone from the office queue
    dash = org.get("/p/dashboard").get_data(as_text=True)
    cancelled = dash[dash.index('data-tab="cancelled">', dash.index("<!-- Cancelled")):]
    assert "To delete" in cancelled                                  # listed under Cancelled only
    assert "Office A" in cancelled                                   # who had it when deleted
    details = org.get(f"/p/document/{lid}")
    assert details.status_code == 200 and b"Delete Request" not in details.data
    assert sig_a.get(f"/p/document/{lid}").status_code == 404
    assert q("SELECT action FROM activity_log WHERE letter_id = ? AND action = 'letter_withdrawn'", (lid,))


def test_next_signer_is_alerted(env):
    org, sig_a, sig_b = Client(env), Client(env), Client(env)
    org.login("/login", "org" + DOMAIN, "password1")
    sig_a.login("/admin/login", "siga" + DOMAIN, "password1")
    sig_b.login("/admin/login", "sigb" + DOMAIN, "password1")
    seen_b = sig_b.get("/api/pulse").json["last_notification"]

    org.post("/upload-document", {"title": "Alert me", "expiry_date": FUTURE, "office_ids": route(),
                                  "document": (make_pdf(), "al.pdf")})
    lid = q("SELECT id FROM letters WHERE title = 'Alert me'")[0]["id"]
    # Office B is second: nothing for B yet
    assert not [n for n in sig_b.get(f"/api/pulse?since={seen_b}").json["new"] if "Alert me" in n["message"]]

    sig_a.post(f"/document/{lid}/action", {"action": "approve_sign", "page_index": "0",
                                           "rel_x": "0.5", "rel_y": "0.7", "rel_w": "0.2"}, ajax=True)
    data = sig_b.get(f"/api/pulse?since={seen_b}").json
    alerts = [n for n in data["new"] if "Alert me" in n["message"]]
    assert alerts and alerts[0]["kind"] == "queue" and alerts[0]["letter_id"] == lid
    assert data["unread_to_sign"] >= 1


def test_uploads_route_is_locked(env):
    anon = Client(env)
    assert anon.get("/uploads/anything.pdf").status_code == 302
    files = q("SELECT file_path FROM letter_versions LIMIT 1")
    import os
    name = os.path.basename(files[0]["file_path"])
    org = Client(env)
    org.login("/login", "org" + DOMAIN, "password1")
    assert org.get("/uploads/" + name).status_code == 404
    assert org.get("/uploads/letters/" + name).status_code == 404


def test_moderator_cannot_deactivate_busy_office(env):
    mod = Client(env)
    mod.login("/moderator/login", "mod" + DOMAIN, "modpass123")
    org = Client(env)
    org.login("/login", "org" + DOMAIN, "password1")
    org.post("/upload-document", {"title": "Busy", "expiry_date": FUTURE, "office_ids": route(), "document": (make_pdf(), "d.pdf")})
    a_id = q("SELECT id FROM offices WHERE name = 'Office A'")[0]["id"]
    mod.post(f"/moderator/offices/{a_id}/toggle")
    assert q("SELECT is_active FROM offices WHERE id = ?", (a_id,))[0]["is_active"] == 1
