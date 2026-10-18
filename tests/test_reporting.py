"""PDF report tests (synthetic case)."""
from __future__ import annotations

import io

import pytest
from PIL import Image
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from sls.core.errors import ReportError, ValidationError
from sls.reporting.arabic import base_direction, visual, wrap_logical, register_fonts
from sls.reporting.pages import parse_page_selection
from sls.reporting.report import ReportOptions
from sls.services.report_service import ReportService

GEN_AT = "2026-10-18T09:00:00+00:00"


def opts(**kw):
    base = dict(language="ar", authority_name="جهة تجريبية", generated_at=GEN_AT, generated_by="tester")
    base.update(kw)
    return ReportOptions(**base)


def text_of(pdf: bytes) -> list[str]:
    return [p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages]


def fonts_of(pdf: bytes) -> set[str]:
    names = set()
    for page in PdfReader(io.BytesIO(pdf)).pages:
        res = page.get("/Resources") or {}
        for f in (res.get("/Font") or {}).values():
            names.add(str(f.get_object().get("/BaseFont")))
    return names


def make_pdf(path, pages=3):
    c = canvas.Canvas(str(path), pagesize=A4)
    for i in range(pages):
        c.drawString(100, 700, f"Synthetic attachment page {i + 1}")
        c.showPage()
    c.save()
    return path


def test_arabic_shaping_and_wrapping_order():
    register_fonts()
    assert base_direction("مرحبا world") == "R" and base_direction("Area 120 م²") == "L"
    v = visual("مساحة", "R")
    assert all(0xFE70 <= ord(ch) <= 0xFEFF for ch in v)  # presentation forms (shaped)
    text = "هذا نص عربي طويل يجب أن يلتف على عدة أسطر مع المحافظة على ترتيب الأسطر الصحيح من البداية"
    lines = wrap_logical(text, 10, 120, "R")
    assert len(lines) > 2
    # First visual line must contain the first logical word (shaped), proving logical-order wrapping.
    assert visual("هذا", "R") in lines[0]


def test_arabic_report_fonts_page_numbers_and_values(full_case):
    svc = ReportService(full_case["ws"])
    res = svc.preview(full_case["case"].id, opts())
    fonts = fonts_of(res.pdf)
    assert any("NotoNaskhArabic" in f for f in fonts) and any("NotoSans" in f for f in fonts), fonts
    pages = text_of(res.pdf)
    assert res.page_count == len(pages) >= 6
    joined = "\n".join(pages)
    assert any(0xFE70 <= ord(ch) <= 0xFEFF for ch in joined)
    assert f"{res.page_count}" in pages[-1]
    assert "448.00" in joined and "268.00" in joined  # measured total, remaining (448 - 180)


def test_english_report_page_numbering_and_tables(full_case):
    res = ReportService(full_case["ws"]).preview(full_case["case"].id, opts(language="en"))
    pages = text_of(res.pdf)
    for i, t in enumerate(pages):
        assert f"Page {i + 1} of {len(pages)}" in t
    joined = "\n".join(pages)
    assert "SYN-2026-042" in joined
    assert "288.00" in joined and "160.00" in joined and "180.00" in joined
    assert "Exceeds limit" in joined  # setback check fails for the synthetic building
    assert "Draft" in joined


def test_drawing_page_scale_and_orientation(full_case):
    svc = ReportService(full_case["ws"])
    res = svc.preview(full_case["case"].id, opts(language="en", drawing_scale=200))
    reader = PdfReader(io.BytesIO(res.pdf))
    drawing_page = reader.pages[1]
    w, h = float(drawing_page.mediabox.width), float(drawing_page.mediabox.height)
    assert w > h and round(w) == 1191  # A3 landscape
    assert "1:200" in drawing_page.extract_text()
    # Vector drawing: content stream has path operators, no raster image on the drawing page.
    content = drawing_page.get_contents().get_data()
    assert b" l\n" in content or b" l " in content
    assert "/XObject" not in (drawing_page.get("/Resources") or {})
    with pytest.raises(ReportError):
        svc.preview(full_case["case"].id, opts(drawing_scale=50, drawing_page_size="A4",
                                               drawing_orientation="portrait"))
    fit = svc.preview(full_case["case"].id, opts(language="en"))
    assert "1:200" in PdfReader(io.BytesIO(fit.pdf)).pages[1].extract_text() or \
        "1:250" in PdfReader(io.BytesIO(fit.pdf)).pages[1].extract_text()


def test_photos_and_attachments_with_page_selection(full_case):
    ws, case, tmp = full_case["ws"], full_case["case"], full_case["tmp"]
    img = tmp / "site.jpg"
    Image.new("RGB", (800, 600), (120, 160, 200)).save(img)
    ws.media.add_photo(ws.user, case.id, img, "الواجهة الأمامية")
    att = ws.media.add_attachment(ws.user, case.id, make_pdf(tmp / "plan.pdf", 3), "مخطط تنظيمي تجريبي")
    ws.media.update_attachment(ws.user, att.id, att.title, True, "2-3")
    lic = full_case["license"]
    scan = tmp / "license_scan.png"
    Image.new("RGB", (600, 800), (240, 240, 230)).save(scan)
    ws.licenses.attach_document(ws.user, lic.id, scan, "ص ١")
    base = ReportService(ws)
    res = base.preview(case.id, opts())
    docs = res.manifest["documents"]
    assert [d["status"] for d in docs] == ["included", "included"]
    plan = next(d for d in docs if d["kind"] == "attachment")
    assert plan["pages"] == [2, 3]
    assert res.manifest["total_pages"] == res.manifest["body_pages"] + 3  # 1 image page + 2 PDF pages
    assert res.manifest["photos"][0]["caption"] == "الواجهة الأمامية"
    assert "Synthetic attachment page 2" in text_of(res.pdf)[res.manifest["body_pages"] + 1]


def test_missing_and_unsupported_attachments_are_reported(full_case):
    ws, case, tmp = full_case["ws"], full_case["case"], full_case["tmp"]
    docx = tmp / "letter.docx"
    docx.write_bytes(b"PK\x03\x04 fake office document")
    ws.media.add_attachment(ws.user, case.id, docx, "خطاب")
    att = ws.media.add_attachment(ws.user, case.id, make_pdf(tmp / "gone.pdf", 1), "ملف مفقود")
    ws.files.path(att.file_id).unlink()  # simulate a file removed from the store
    res = ReportService(ws).preview(case.id, opts(language="en"))
    statuses = {d["file"]: (d["status"], d["error"]) for d in res.manifest["documents"]}
    assert statuses["letter.docx"][0] == "not_included" and "cannot be rendered" in statuses["letter.docx"][1]
    assert statuses["gone.pdf"][0] == "not_included" and "missing" in statuses["gone.pdf"][1]
    assert len(res.warnings) == 2
    assert "NOT included" in "\n".join(text_of(res.pdf))


def test_invalid_page_selection():
    assert parse_page_selection("", 5) is None
    assert parse_page_selection("1,3-4", 5) == [0, 2, 3]
    with pytest.raises(ValidationError):
        parse_page_selection("4-2", 5)
    with pytest.raises(ValidationError):
        parse_page_selection("7", 5)
    with pytest.raises(ValidationError):
        parse_page_selection("a", None)


def test_report_generation_is_reproducible(full_case):
    svc = ReportService(full_case["ws"])
    a = svc.preview(full_case["case"].id, opts())
    b = svc.preview(full_case["case"].id, opts())
    assert a.pdf == b.pdf


def test_export_saves_revision_and_enforces_approval(full_case):
    from conftest import SURVEYOR

    ws, case = full_case["ws"], full_case["case"]
    svc = ReportService(ws)
    exp1, _ = svc.export(case.id, opts())
    exp2, _ = svc.export(case.id, opts(status="approved"))
    assert (exp1.revision, exp2.revision) == (1, 2)
    assert [r.status for r in ws.reports.list(case.id)] == ["approved", "draft"]
    assert ws.files.path(exp2.file_id).read_bytes()[:4] == b"%PDF"
    ws.user = SURVEYOR
    from sls.core.errors import PermissionDeniedError
    with pytest.raises(PermissionDeniedError):
        svc.export(case.id, opts(status="approved"))


def test_approval_blocked_when_case_changed_or_unverified(full_case):
    from sls.persistence.licenses_repo import LicensedArea, LicenseRecord

    ws, case = full_case["ws"], full_case["case"]
    svc = ReportService(ws)
    ws.licenses.save(ws.user, LicenseRecord(case.id, "SYN-LIC-002", areas=[LicensedArea("1", 20.0)]))
    blockers = svc.approval_blockers(case.id, "approved")
    assert any("Unverified license" in b for b in blockers)
    assert any("recalculate" in b for b in blockers)
    with pytest.raises(ValidationError):
        svc.export(case.id, opts(status="approved"))
