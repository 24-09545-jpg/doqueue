# pdfsign.py
"""
PDF signature rendering.

Placements are stored as fractions of the *displayed* page (the same
coordinate space the browser preview uses, via pdf.js): (rel_x, rel_y) is the
top-left corner of the signature box, (rel_w, rel_h) its size. Because the
final PDF is always re-rendered from the stored placements, signatures land in
exactly the same spot in the downloaded / printed file as in the preview.
"""
import os
from io import BytesIO

from PyPDF2 import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import filestore

MIN_REL_W = 0.03
MAX_REL_W = 0.9


# ───────────── page geometry ─────────────

def _page_geometry(page):
    """Return (x0, y0, w, h, rotation) of the visible (crop) box."""
    box = page.cropbox
    x0, y0 = float(box.left), float(box.bottom)
    w, h = float(box.width), float(box.height)
    rot = int(page.get("/Rotate", 0) or 0) % 360
    return x0, y0, w, h, rot


def displayed_size(page):
    """Width/height of the page as the viewer shows it (accounts for /Rotate)."""
    _, _, w, h, rot = _page_geometry(page)
    return (h, w) if rot in (90, 270) else (w, h)


def page_count(pdf_path):
    return len(PdfReader(filestore.ensure(pdf_path)).pages)


def _apply_display_transform(c, page):
    """
    After this call, drawing on canvas `c` uses displayed-page coordinates
    (origin bottom-left of the page as seen on screen, units = points).
    """
    x0, y0, w, h, rot = _page_geometry(page)
    if rot == 90:
        c.transform(0, 1, -1, 0, x0 + w, y0)
    elif rot == 180:
        c.transform(-1, 0, 0, -1, x0 + w, y0 + h)
    elif rot == 270:
        c.transform(0, -1, 1, 0, x0, y0 + h)
    else:
        c.translate(x0, y0)


def normalize_placement(pdf_path, image_path, page_index, rel_x, rel_y, rel_w):
    """
    Validate and clamp a placement, deriving rel_h from the image's own aspect
    ratio so the signature always keeps its proportions.
    Returns dict(page_index, rel_x, rel_y, rel_w, rel_h).
    """
    reader = PdfReader(filestore.ensure(pdf_path))
    filestore.ensure(image_path)
    n = len(reader.pages)
    page_index = int(page_index)
    if page_index != -1 and not (0 <= page_index < n):
        raise ValueError("Invalid page.")
    ref_page = reader.pages[0 if page_index == -1 else page_index]
    dw, dh = displayed_size(ref_page)

    iw, ih = ImageReader(image_path).getSize()
    if iw <= 0 or ih <= 0:
        raise ValueError("Invalid signature image.")

    rel_w = max(MIN_REL_W, min(MAX_REL_W, float(rel_w)))
    rel_h = rel_w * dw * (ih / iw) / dh
    if rel_h > 0.9:  # extremely tall image: shrink to fit
        rel_h = 0.9
        rel_w = rel_h * dh * (iw / ih) / dw
    rel_x = max(0.0, min(1.0 - rel_w, float(rel_x)))
    rel_y = max(0.0, min(1.0 - rel_h, float(rel_y)))
    return {"page_index": page_index, "rel_x": rel_x, "rel_y": rel_y, "rel_w": rel_w, "rel_h": rel_h}


# ───────────── rendering ─────────────

def render_pdf(base_pdf_path, placements, out_path, tracking_number=None):
    """
    Render `placements` (iterable of dicts with image_path, page_index, rel_x,
    rel_y, rel_w, rel_h) on top of `base_pdf_path` and write `out_path`.
    If `tracking_number` is given, it is stamped small at the bottom-left of
    every page.
    """
    reader = PdfReader(filestore.ensure(base_pdf_path))
    placements = list(placements)
    for p in placements:
        filestore.ensure(p["image_path"])
    writer = PdfWriter()

    for i, page in enumerate(reader.pages):
        on_page = [p for p in placements if int(p["page_index"]) in (i, -1)]
        if on_page or tracking_number:
            mb = page.mediabox
            packet = BytesIO()
            c = canvas.Canvas(packet, pagesize=(float(mb.right), float(mb.top)))
            c.saveState()
            _apply_display_transform(c, page)
            dw, dh = displayed_size(page)

            for p in on_page:
                img = ImageReader(p["image_path"])
                w = float(p["rel_w"]) * dw
                h = float(p["rel_h"]) * dh
                x = float(p["rel_x"]) * dw
                y = dh - float(p["rel_y"]) * dh - h
                # mask='auto' keeps PNG transparency, so no white box covers the text
                c.drawImage(img, x, y, width=w, height=h, mask="auto")

            if tracking_number:
                c.setFont("Helvetica", 7)
                c.setFillColorRGB(0.25, 0.25, 0.25)
                c.drawString(14, 10, f"Tracking No.: {tracking_number}")

            c.restoreState()
            c.showPage()
            c.save()
            packet.seek(0)
            page.merge_page(PdfReader(packet).pages[0])
        writer.add_page(page)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        writer.write(f)
    os.replace(tmp, out_path)
    filestore.save(out_path)
    return out_path


# ───────────── signature images ─────────────

def prepare_signature_image(src, dest_path, remove_background=True, threshold=235):
    """
    Normalise an uploaded signature into an RGBA PNG:
      - images without transparency get their light background made transparent
        (so the signature never covers document text),
      - empty margins are trimmed so the drag/resize box hugs the ink.
    `src` may be a path or a file-like object.
    """
    from PIL import Image, ImageChops

    img = Image.open(src)
    img.load()
    img = img.convert("RGBA")

    alpha = img.getchannel("A")
    has_transparency = alpha.getextrema()[0] < 250

    if remove_background and not has_transparency:
        soft = max(threshold - 40, 0)

        def _alpha_for(lum):
            if lum >= threshold:
                return 0
            if lum <= soft:
                return 255
            # feather anti-aliased edges
            return int(255 * (threshold - lum) / float(threshold - soft))

        bg_mask = img.convert("L").point(_alpha_for)
        img.putalpha(ImageChops.multiply(alpha, bg_mask))

    bbox = img.getchannel("A").getbbox()
    if bbox:
        pad = 4
        l, t, r, b = bbox
        img = img.crop((max(l - pad, 0), max(t - pad, 0),
                        min(r + pad, img.width), min(b + pad, img.height)))
    elif remove_background:
        raise ValueError("The signature image appears to be blank.")

    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    img.save(dest_path, "PNG")
    filestore.save(dest_path)
    return dest_path
