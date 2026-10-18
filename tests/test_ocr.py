"""Optional local OCR (skipped when Tesseract is not installed). Output stays unverified."""
from __future__ import annotations



import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from sls.licensing.ocr import OcrUnavailableError, find_tesseract, ocr_file
from sls.reporting.arabic import FONT_DIR, draw_text

needs_tesseract = pytest.mark.skipif(find_tesseract() is None, reason="Tesseract OCR not installed")


@needs_tesseract
def test_ocr_image_english(tmp_path):
    img = Image.new("RGB", (1400, 300), "white")
    font = ImageFont.truetype(str(FONT_DIR / "NotoSans-Regular.ttf"), 64)
    ImageDraw.Draw(img).text((40, 100), "BUILDING LICENSE 2741", fill="black", font=font)
    path = tmp_path / "scan.png"
    img.save(path)
    text, langs = ocr_file(path, languages="eng")
    assert "LICENSE" in text and "2741" in text and langs == "eng"


@needs_tesseract
def test_ocr_pdf_arabic_and_stored_unverified(tmp_path, full_case):
    path = tmp_path / "license.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    draw_text(c, 500, 700, "رخصة بناء رقم ٥٥", 28, align="right")
    c.showPage()
    c.save()
    text, langs = ocr_file(path)
    assert "ara" in langs
    assert "رخصة" in text or "بناء" in text
    ws = full_case["ws"]
    lic = full_case["license"]
    doc = ws.licenses.attach_document(ws.user, lic.id, path)
    ws.licenses.add_ocr_text(doc.file_id, "tesseract", langs, text)
    stored = ws.licenses.ocr_texts(doc.file_id)
    assert stored[0]["reviewed"] == 0
    # OCR never changes license values.
    assert ws.licenses.get(lic.id).areas == lic.areas


def test_unsupported_and_missing_engine(tmp_path):
    with pytest.raises(OcrUnavailableError):
        ocr_file(tmp_path / "x.png", configured=str(tmp_path / "no-tesseract.exe"))
    if find_tesseract() is not None:
        (tmp_path / "x.docx").write_bytes(b"x")
        with pytest.raises(OcrUnavailableError):
            ocr_file(tmp_path / "x.docx")
