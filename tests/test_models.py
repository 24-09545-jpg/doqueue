"""Unit tests for the data layer and PDF/signature helpers (use a throwaway database)."""
import io
import os

import pytest
from PIL import Image


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DSA_DB_PATH", str(tmp_path / "t.sqlite3"))
    import db
    db.reset_module_connections()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite3"))
    db.init_db(seed_moderator=False)
    yield db
    db.reset_module_connections()


def test_create_and_find_user(fresh_db):
    import models
    org_id = models.create_org("Org A")
    uid = models.create_user("a@g.batstate-u.edu.ph", "password123", "A", "user", organization_id=org_id)
    u = models.find_user_by_id(uid)
    assert u.email == "a@g.batstate-u.edu.ph"
    assert u.organization_name == "Org A"
    assert models.verify_password(u, "password123")
    assert not models.verify_password(u, "wrong")
    assert u["password_hash"] != "password123"  # never stored in plain text


def test_default_content_seeded(fresh_db):
    import models
    rows = models.parse_contact_rows(models.get_content("contacts_rows"))
    assert ("Alangilan", "ict.alangilan@g.batstate-u.edu.ph") in rows


def test_signature_background_made_transparent(tmp_path):
    import pdfsign
    img = Image.new("RGB", (200, 80), "white")
    for x in range(20, 180):
        img.putpixel((x, 40), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    out = pdfsign.prepare_signature_image(buf, str(tmp_path / "sig.png"))
    res = Image.open(out)
    assert res.mode == "RGBA"
    assert res.getpixel((0, 0))[3] == 0          # background removed
    assert res.width < 200 and res.height < 80   # trimmed to the ink


def test_render_keeps_aspect_and_stamps_tracking(tmp_path):
    import pdfsign
    from PyPDF2 import PdfReader
    from reportlab.pdfgen import canvas

    pdf = str(tmp_path / "a.pdf")
    c = canvas.Canvas(pdf, pagesize=(612, 792))
    c.drawString(100, 700, "hello")
    c.showPage()
    c.save()
    sig = str(tmp_path / "s.png")
    Image.new("RGBA", (400, 100), (0, 0, 0, 255)).save(sig)

    p = pdfsign.normalize_placement(pdf, sig, 0, 0.5, 0.5, 0.25)
    # height derived from the image ratio (100/400) in page units
    assert abs(p["rel_h"] * 792 - p["rel_w"] * 612 * 0.25) < 0.01
    p["image_path"] = sig
    out = pdfsign.render_pdf(pdf, [p], str(tmp_path / "o.pdf"), tracking_number="DSA-2026-0001")
    text = PdfReader(out).pages[0].extract_text()
    assert "DSA-2026-0001" in text
