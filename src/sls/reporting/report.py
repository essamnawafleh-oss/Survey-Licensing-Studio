"""Multipage licensing package PDF.

Pipeline: preflight attachments -> body (ReportLab, vector drawing) -> merge attachment
pages (pypdf) -> stamp page numbers and status on every page. Attachments that cannot be
included are listed in the PDF, in the manifest and in the returned warnings.
"""
from __future__ import annotations

import io
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, A4, landscape, portrait
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (BaseDocTemplate, Frame, Image as RLImage, KeepTogether, NextPageTemplate,
                                PageBreak, PageTemplate, Spacer, Table, TableStyle)
from reportlab.platypus.flowables import Flowable

from ..cad.model import Drawing
from ..core.errors import ReportError, ValidationError
from ..core.i18n import tr
from ..core.settings import NumberFormat
from .arabic import BidiText, TextStyle, draw_text, register_fonts
from .drawing_pdf import Viewport, choose_scale, draw_north_arrow, draw_scale_bar, paper_points_per_unit, render_drawing
from .pages import parse_page_selection

PAGE_SIZES = {"A4": A4, "A3": A3}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
STATUS_COLORS = {"draft": (0.75, 0.1, 0.1), "reviewed": (0.8, 0.5, 0.0), "approved": (0.1, 0.5, 0.2)}


@dataclass
class ReportOptions:
    language: str = "ar"
    page_size: str = "A4"
    orientation: str = "portrait"
    drawing_page_size: str = "A3"
    drawing_orientation: str = "landscape"
    drawing_scale: int = 0  # 0 = fit to a standard scale
    margins_mm: float = 15.0
    authority_name: str = ""
    report_title: str = ""
    header_text: str = ""
    footer_text: str = ""
    monochrome_drawing: bool = False
    include_sensitive_fields: bool = False
    include_trace: bool = True
    status: str = "draft"
    generated_at: str = ""      # explicit, for reproducible output
    generated_by: str = ""
    revision: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceDocument:
    kind: str           # license_doc | attachment
    title: str
    path: Path
    sha256: str
    page_selection: str = ""
    reference: str = ""  # e.g. license number
    file_name: str = ""  # original file name (the stored copy carries a hash prefix)


@dataclass
class PhotoItem:
    path: Path
    caption: str
    sha256: str


@dataclass
class ReportData:
    case: dict[str, Any]              # flat case metadata (already formatted strings)
    custom_fields: list[tuple[str, str, bool]]  # (label, value, sensitive)
    rule_version: dict[str, Any] | None
    calculation: dict[str, Any] | None  # {"id","revision","input_hash","result_hash","results"}
    licenses: list[dict[str, Any]]
    notes: list[tuple[str, str]]      # (section, text)
    drawing: Drawing | None
    unit_to_m: float | None
    photos: list[PhotoItem] = field(default_factory=list)
    documents: list[SourceDocument] = field(default_factory=list)


@dataclass
class ReportResult:
    pdf: bytes
    manifest: dict[str, Any]
    warnings: list[str]
    page_count: int


# ---------------------------------------------------------------------------- helpers


class _Ctx:
    def __init__(self, opts: ReportOptions, nf: NumberFormat) -> None:
        self.o = opts
        self.lang = opts.language
        self.rtl = opts.language == "ar"
        self.nf = nf

    def t(self, key: str, **kw: object) -> str:
        return tr(key, self.lang, **kw)

    def p(self, text: Any, size: float = 9.0, bold: bool = False, align: str = "auto",
          color: tuple[float, float, float] = (0.1, 0.1, 0.1), space_after: float = 0.0) -> BidiText:
        return BidiText("" if text is None else str(text),
                        TextStyle(size=size, bold=bold, align=align, color=color, space_after=space_after),
                        rtl_default=self.rtl)

    def h(self, text: str, level: int = 1) -> BidiText:
        size = {1: 15, 2: 12, 3: 10.5}[level]
        return self.p(text, size=size, bold=True, color=(0.05, 0.2, 0.35), space_after=4)

    def btype(self, key: str) -> str:
        """Translate standard boundary types; custom types from the rule table are shown as-is."""
        label = self.t(f"btype.{key}")
        return key if label == f"btype.{key}" else label

    def optional(self, key: str, fallback: str) -> str:
        label = self.t(key)
        return fallback if label == key else label

    def area(self, v: float | None) -> str:
        return "—" if v is None else self.nf.area(v)

    def length(self, v: float | None) -> str:
        return "—" if v is None else self.nf.length(v)

    def table(self, header: list[str], rows: list[list[Any]], widths: list[float], zebra: bool = True) -> Table:
        data = [[self.p(h, 8.5, bold=True, color=(1, 1, 1)) for h in header]]
        for r in rows:
            data.append([c if isinstance(c, Flowable) else self.p(c, 8.5) for c in r])
        if self.rtl:
            data = [list(reversed(r)) for r in data]
            widths = list(reversed(widths))
        tbl = Table(data, colWidths=widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A52")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#8A9AA8")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]
        if zebra:
            for i in range(1, len(data)):
                if i % 2 == 0:
                    style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#EEF2F5")))
        tbl.setStyle(TableStyle(style))
        return tbl

    def kv(self, pairs: list[tuple[str, Any]], width: float) -> Table:
        rows = [[self.p(k, 8.5, bold=True), self.p(v, 8.5)] for k, v in pairs]
        widths = [width * 0.35, width * 0.65]
        if self.rtl:
            rows = [list(reversed(r)) for r in rows]
            widths = list(reversed(widths))
        tbl = Table(rows, colWidths=widths)
        tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#8A9AA8")),
                                 ("BACKGROUND", (1 if self.rtl else 0, 0), (1 if self.rtl else 0, -1),
                                  colors.HexColor("#E3EAF0")),
                                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        return tbl


class _DrawingFlowable(Flowable):
    """Full-frame vector drawing with scale bar, north arrow and title block."""

    def __init__(self, ctx: _Ctx, data: ReportData, scale_n: int, w: float, h: float) -> None:
        super().__init__()
        self.ctx, self.data, self.scale_n, self.w, self.h = ctx, data, scale_n, w, h

    def wrap(self, aw: float, ah: float) -> tuple[float, float]:
        return self.w, self.h

    def draw(self) -> None:
        c = self.canv
        ctx, data = self.ctx, self.data
        assert data.drawing is not None and data.unit_to_m is not None
        tb_h = 30 * mm
        c.setLineWidth(0.8)
        c.rect(0, 0, self.w, self.h)
        c.line(0, tb_h, self.w, tb_h)
        ext = data.drawing.extents()
        assert ext is not None
        k = paper_points_per_unit(data.unit_to_m, self.scale_n)
        vp = Viewport(2 * mm, tb_h + 2 * mm, self.w - 4 * mm, self.h - tb_h - 4 * mm,
                      (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2, k)
        render_drawing(c, data.drawing, vp, data.unit_to_m, self.scale_n, ctx.o.monochrome_drawing)
        draw_north_arrow(c, self.w - 12 * mm, self.h - 18 * mm)
        draw_scale_bar(c, 8 * mm, tb_h + 5 * mm, self.scale_n)
        # Title block
        case = data.case
        cells = [
            (ctx.t("rpt.case_number"), case.get("case_number", "")),
            (ctx.t("rpt.parcel"), case.get("parcel_identifier", "")),
            (ctx.t("rpt.scale"), f"1:{self.scale_n}"),
            (ctx.t("rpt.units_crs"), f"{case.get('units', '')} / {case.get('crs', '')}"),
            (ctx.t("rpt.surveyor"), case.get("surveyor", "")),
            (ctx.t("rpt.status"), ctx.t(f"status.{ctx.o.status}")),
        ]
        col_w = self.w / 3
        for i, (label, value) in enumerate(cells):
            col, row = i % 3, i // 3
            x = col_w * (2 - col) if ctx.rtl else col_w * col
            y = tb_h - (row + 1) * 13 * mm
            c.setFillColorRGB(0.35, 0.35, 0.35)
            if ctx.rtl:
                draw_text(c, x + col_w - 3 * mm, y + 7 * mm, label, 7, align="right")
                c.setFillColorRGB(0, 0, 0)
                draw_text(c, x + col_w - 3 * mm, y + 1.5 * mm, str(value), 9.5, align="right", bold=True)
            else:
                draw_text(c, x + 3 * mm, y + 7 * mm, label, 7, rtl_default=False)
                c.setFillColorRGB(0, 0, 0)
                draw_text(c, x + 3 * mm, y + 1.5 * mm, str(value), 9.5, bold=True, rtl_default=False)
        for i in (1, 2):
            c.line(col_w * i, 0, col_w * i, tb_h)
        c.line(0, tb_h / 2 + 2.5 * mm, self.w, tb_h / 2 + 2.5 * mm)
        c.setFillColorRGB(0.3, 0.3, 0.3)
        draw_text(c, self.w - 3 * mm if ctx.rtl else 3 * mm, tb_h + 1.5 * mm, ctx.t("rpt.grid_north_note"), 6.5,
                  align="right" if ctx.rtl else "left", rtl_default=ctx.rtl)


# --------------------------------------------------------------------------- preflight


def _preflight(docs: list[SourceDocument]) -> list[dict[str, Any]]:
    """Check every selected document before the body is built (so failures can be listed)."""
    from ..persistence.filestore import sha256_file

    entries: list[dict[str, Any]] = []
    for d in docs:
        e: dict[str, Any] = {"kind": d.kind, "title": d.title, "file": d.file_name or d.path.name, "sha256": d.sha256,
                             "reference": d.reference, "page_selection": d.page_selection or "all",
                             "status": "included", "error": "", "pages": []}
        suffix = d.path.suffix.lower()
        try:
            if not d.path.is_file():
                raise ReportError("file is missing from the case store")
            if sha256_file(d.path) != d.sha256:
                raise ReportError("file content does not match its recorded checksum")
            if suffix == ".pdf":
                reader = PdfReader(str(d.path))
                if reader.is_encrypted:
                    raise ReportError("PDF is encrypted")
                sel = parse_page_selection(d.page_selection, len(reader.pages))
                e["pages"] = [i + 1 for i in (sel if sel is not None else range(len(reader.pages)))]
                e["type"] = "pdf"
            elif suffix in IMAGE_SUFFIXES:
                if d.page_selection:
                    raise ReportError("page selection is not applicable to an image")
                with Image.open(d.path) as im:
                    im.verify()
                e["pages"] = [1]
                e["type"] = "image"
            else:
                raise ReportError(f"file type '{suffix or '?'}' cannot be rendered into the PDF offline "
                                  "(convert it to PDF first)")
        except (ReportError, ValidationError) as exc:
            e["status"], e["error"] = "not_included", exc.message
        except (PdfReadError, OSError, Image.UnidentifiedImageError, ValueError) as exc:
            e["status"], e["error"] = "not_included", f"unreadable file: {exc}"
        entries.append(e)
    return entries


# -------------------------------------------------------------------------------- body


def _body(data: ReportData, opts: ReportOptions, nf: NumberFormat, manifest_entries: list[dict[str, Any]],
          warnings: list[str]) -> bytes:
    register_fonts()
    ctx = _Ctx(opts, nf)
    text_size = PAGE_SIZES[opts.page_size]
    text_size = landscape(text_size) if opts.orientation == "landscape" else portrait(text_size)
    dsize = PAGE_SIZES[opts.drawing_page_size]
    dsize = landscape(dsize) if opts.drawing_orientation == "landscape" else portrait(dsize)
    m = opts.margins_mm * mm
    header_space = 10 * mm
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=text_size, leftMargin=m, rightMargin=m, topMargin=m, bottomMargin=m,
                          title=f"{opts.report_title or tr('rpt.title', opts.language)} {data.case.get('case_number', '')}",
                          author=opts.generated_by, creator="Survey & Licensing Studio", invariant=1)
    tw, th = text_size[0] - 2 * m, text_size[1] - 2 * m - header_space
    dw, dh = dsize[0] - 2 * m, dsize[1] - 2 * m - header_space
    doc.addPageTemplates([
        PageTemplate("text", [Frame(m, m, tw, th, id="t", leftPadding=0, rightPadding=0, topPadding=0,
                                    bottomPadding=0)], pagesize=text_size),
        PageTemplate("drawing", [Frame(m, m, dw, dh, id="d", leftPadding=0, rightPadding=0, topPadding=0,
                                       bottomPadding=0)], pagesize=dsize),
    ])
    story: list[Any] = []
    results = (data.calculation or {}).get("results") or {}
    case = data.case

    # 1. Cover ---------------------------------------------------------------------
    story.append(ctx.p(opts.authority_name, 12, bold=True, align="center", color=(0.2, 0.2, 0.2)))
    story.append(Spacer(1, 18 * mm))
    story.append(ctx.p(opts.report_title or ctx.t("rpt.title"), 22, bold=True, align="center",
                       color=(0.05, 0.2, 0.35), space_after=4))
    story.append(ctx.p(f"{ctx.t('rpt.case_number')}: {case.get('case_number', '')}", 14, align="center"))
    story.append(Spacer(1, 12 * mm))
    pairs = [
        (ctx.t("rpt.parcel"), case.get("parcel_identifier", "")),
        (ctx.t("rpt.case_status"), case.get("status_label", "")),
        (ctx.t("rpt.survey_date"), case.get("survey_date", "")),
        (ctx.t("rpt.surveyor"), case.get("surveyor", "")),
        (ctx.t("rpt.land_use"), case.get("land_use", "")),
        (ctx.t("rpt.units_crs"), f"{case.get('units', '')} / {case.get('crs', '')}"),
    ]
    for label, value, sensitive in data.custom_fields:
        if value and (not sensitive or opts.include_sensitive_fields):
            pairs.append((label, value))
    rv = data.rule_version
    if rv:
        pairs.append((ctx.t("rpt.rule_version"), f"{rv['label']} — SHA-256 {rv['sha256'][:16]}…"))
    if data.calculation:
        pairs.append((ctx.t("rpt.calculation"), f"#{data.calculation['revision']} — "
                                                 f"{ctx.t('rpt.input_hash')} {data.calculation['input_hash'][:16]}…"))
    pairs += [(ctx.t("rpt.report_revision"), str(opts.revision)),
              (ctx.t("rpt.status"), ctx.t(f"status.{opts.status}")),
              (ctx.t("rpt.generated"), f"{opts.generated_at} — {opts.generated_by}")]
    story.append(ctx.kv(pairs, tw))
    if opts.status != "approved":
        story.append(Spacer(1, 8 * mm))
        story.append(ctx.p(ctx.t("rpt.not_approved_notice"), 9, bold=True, align="center", color=(0.7, 0.1, 0.1)))

    # 2. Drawing ---------------------------------------------------------------------
    if data.drawing is not None and data.unit_to_m and data.drawing.extents():
        ext = data.drawing.extents()
        assert ext is not None
        tb_h = 30 * mm
        scale = choose_scale(ext, data.unit_to_m, dw - 4 * mm, dh - tb_h - 4 * mm, opts.drawing_scale)
        story += [NextPageTemplate("drawing"), PageBreak(), _DrawingFlowable(ctx, data, scale, dw, dh),
                  NextPageTemplate("text"), PageBreak()]
    else:
        warnings.append(ctx.t("rpt.no_drawing"))
        story += [PageBreak(), ctx.h(ctx.t("rpt.sec_drawing")), ctx.p(ctx.t("rpt.no_drawing")), PageBreak()]

    # 3. Setbacks and areas --------------------------------------------------------------
    story.append(ctx.h(ctx.t("rpt.sec_setbacks")))
    if not results:
        story.append(ctx.p(ctx.t("rpt.no_calculation"), color=(0.7, 0.1, 0.1)))
        warnings.append(ctx.t("rpt.no_calculation"))
    else:
        if results.get("errors"):
            story.append(ctx.p(ctx.t("rpt.calc_errors"), bold=True, color=(0.7, 0.1, 0.1)))
            for e in results["errors"]:
                story.append(ctx.p(f"• [{e['section']}] {e['message']}", 8.5, color=(0.7, 0.1, 0.1)))
            story.append(Spacer(1, 3 * mm))
        rule = results.get("rule")
        if rule:
            override = (results.get("rule_override") or {}).get("setbacks") or {}
            raw = rule.get("setbacks_raw") or {k: f"{v:g}" for k, v in rule["setbacks"].items()}
            rows = []
            for k in raw:
                if k in rule["setbacks"]:
                    applied, basis = ctx.length(rule["setbacks"][k]), ctx.t("rpt.basis_rule")
                elif k in override:
                    applied, basis = ctx.length(override[k]), ctx.t("rpt.basis_reviewer")
                else:
                    applied, basis = "—", ctx.t("rpt.basis_none")
                rows.append([ctx.btype(k), raw[k] or "—", applied, basis])
            name = rule.get("name_ar") if ctx.rtl else (rule.get("name_en") or rule.get("name_ar"))
            story.append(ctx.p(f"{ctx.t('rpt.applied_rule')}: {rule['code']} — {name}  "
                               f"({ctx.t('rpt.source_row')} {rule.get('source_row', '')})", 9.5, bold=True))
            story.append(ctx.table([ctx.t("rpt.boundary_type"), ctx.t("rpt.rule_text"), ctx.t("rpt.setback_m"),
                                    ctx.t("rpt.basis")], rows, [tw * 0.2, tw * 0.35, tw * 0.15, tw * 0.3]))
            extra = []
            if rule.get("coverage_raw"):
                extra.append((ctx.t("check.coverage"), rule["coverage_raw"]))
            if rule.get("floors_raw"):
                extra.append((ctx.t("check.floors"), rule["floors_raw"]))
            if rule.get("notes"):
                extra.append((ctx.t("rules.field.notes"), rule["notes"]))
            for key, value in (rule.get("source") or {}).items():
                if key.upper().startswith("REMARK_") and value:
                    extra.append((key, value))
            if override:
                ov = results.get("rule_override") or {}
                extra.append((ctx.t("rpt.reviewer_values"),
                              f"{ov.get('note', '')} — {ov.get('entered_by', '')} {ov.get('entered_at', '')}"))
            if extra:
                story.append(Spacer(1, 2 * mm))
                story.append(ctx.kv(extra, tw))
            story.append(Spacer(1, 3 * mm))
        parcel = results.get("parcel")
        sb = results.get("setback")
        if parcel:
            story.append(ctx.kv([(ctx.t("rpt.parcel_area"), f"{ctx.area(parcel['area_m2'])} m²"),
                                 (ctx.t("rpt.parcel_perimeter"), f"{ctx.length(parcel['perimeter_m'])} m"),
                                 (ctx.t("rpt.buildable_area"),
                                  f"{ctx.area(sb['buildable_area_m2'])} m²" if sb else "—"),
                                 (ctx.t("rpt.setback_status"), ctx.t(f"setback.{sb['status']}") if sb else "—")], tw))
            story.append(Spacer(1, 3 * mm))
        if sb:
            rows = [[str(e["index"]), ctx.btype(e["boundary_type"]), ctx.length(e["length_m"]),
                     ctx.length(e["setback_m"])]
                    for e in sb["edges"]]
            story.append(ctx.table([ctx.t("rpt.edge"), ctx.t("rpt.boundary_type"), ctx.t("rpt.edge_length_m"),
                                    ctx.t("rpt.setback_m")], rows, [tw * 0.15, tw * 0.35, tw * 0.25, tw * 0.25]))
            for w in sb.get("warnings", []):
                story.append(ctx.p(f"! {w}", 8.5, color=(0.6, 0.35, 0)))
        story.append(Spacer(1, 5 * mm))
        story.append(ctx.h(ctx.t("rpt.sec_areas"), 2))
        rows = []
        for f in results.get("floors", []):
            rows.append([f["floor_key"], ctx.area(f["area_m2"]), ctx.area(f["overlap_m2"]),
                         ctx.area(f.get("in_setback_zone_m2")), ctx.area(f.get("outside_parcel_m2"))])
        totals = results.get("totals", {})
        rows.append([ctx.t("rpt.total"), ctx.area(totals.get("measured_total_area_m2")), "", "", ""])
        story.append(ctx.table([ctx.t("rpt.floor"), ctx.t("rpt.measured_area"), ctx.t("rpt.overlap_removed"),
                                ctx.t("rpt.in_setback"), ctx.t("rpt.outside_parcel")], rows,
                               [tw * 0.16, tw * 0.21, tw * 0.21, tw * 0.21, tw * 0.21]))

        # 4. Walls and fences
        story.append(Spacer(1, 5 * mm))
        story.append(ctx.h(ctx.t("rpt.sec_lengths"), 2))
        rec = results.get("reconciliation", {})
        lrows = [[ctx.t(f"kind.{x['key']}"), ctx.length(x["measured"]), ctx.length(x["licensed"]),
                  ctx.length(x["remaining"]), "; ".join(x["conflicts"] + x["warnings"])] for x in rec.get("lengths", [])]
        story.append(ctx.table([ctx.t("rpt.element"), ctx.t("rpt.measured_m"), ctx.t("rpt.licensed_m"),
                                ctx.t("rpt.remaining_m"), ctx.t("rpt.remarks")], lrows or [["—", "", "", "", ""]],
                               [tw * 0.15, tw * 0.17, tw * 0.17, tw * 0.17, tw * 0.34]))

    # 5. Previous licenses ---------------------------------------------------------------
    story += [PageBreak(), ctx.h(ctx.t("rpt.sec_licenses"))]
    if not data.licenses:
        story.append(ctx.p(ctx.t("rpt.no_licenses"), color=(0.6, 0.35, 0)))
    for lic in data.licenses:
        pairs = [(ctx.t("rpt.license_number"), lic["license_number"]),
                 (ctx.t("rpt.issue_date"), lic["issue_date"] or "—"),
                 (ctx.t("rpt.structure"), lic["structure_description"] or "—"),
                 (ctx.t("rpt.licensed_areas"), "; ".join(f"{a['floor_key']}: {ctx.area(a['area_m2'])} m²"
                                                         for a in lic["areas"]) or "—"),
                 (ctx.t("rpt.licensed_lengths"), "; ".join(f"{ctx.t('kind.' + x['kind'])}: {ctx.length(x['length_m'])} m"
                                                           for x in lic["lengths"]) or "—"),
                 (ctx.t("rpt.approved_violations"), "; ".join(v["description"] for v in lic["violations"]) or "—"),
                 (ctx.t("rpt.fees"), f"{lic['fee_amount']} {lic['fee_currency']}" if lic["fee_amount"] is not None
                  else "—"),
                 (ctx.t("rpt.verification"), f"{ctx.t('verif.' + lic['verification_status'])} "
                                             f"{lic['verified_by']} {lic['verified_at']}".strip()),
                 (ctx.t("rpt.documents"), "; ".join(f"{d['original_name']} {d['page_refs']}".strip()
                                                    for d in lic["documents"]) or "—")]
        if lic.get("issues"):
            pairs.append((ctx.t("rpt.flags"), "; ".join(ctx.t(k) for k in lic["issues"])))
        story += [KeepTogether([ctx.kv(pairs, tw)]), Spacer(1, 4 * mm)]

    # 6. Licensed vs remaining ---------------------------------------------------------------
    if results:
        rec = results.get("reconciliation", {})
        story += [Spacer(1, 3 * mm), ctx.h(ctx.t("rpt.sec_remaining"), 2)]
        rows = [[x["key"], ctx.area(x["measured"]), ctx.area(x["licensed"]), ctx.area(x["remaining"]),
                 "; ".join(x["conflicts"] + x["warnings"])] for x in rec.get("areas", [])]
        story.append(ctx.table([ctx.t("rpt.floor"), ctx.t("rpt.measured_area"), ctx.t("rpt.licensed_area"),
                                ctx.t("rpt.remaining_area"), ctx.t("rpt.remarks")], rows or [["—", "", "", "", ""]],
                               [tw * 0.12, tw * 0.17, tw * 0.17, tw * 0.17, tw * 0.37]))
        totals = results.get("totals", {})
        story.append(Spacer(1, 3 * mm))
        story.append(ctx.p(ctx.t("rpt.categories_note"), 8.5, color=(0.3, 0.3, 0.3)))
        story.append(ctx.kv([
            (ctx.t("cat.measured"), f"{ctx.area(totals.get('measured_total_area_m2'))} m²"),
            (ctx.t("cat.licensed"), f"{ctx.area(totals.get('previously_licensed_area_m2'))} m²"),
            (ctx.t("cat.remaining"), f"{ctx.area(totals.get('remaining_to_assess_m2'))} m²"
             if totals.get("remaining_to_assess_m2") is not None else f"— ({rec.get('total_remaining_reason', '')})"),
            (ctx.t("cat.violation"), f"{ctx.area(totals.get('setback_violation_area_m2'))} m²"),
            (ctx.t("cat.eligible"), ctx.t("rpt.eligible_not_determined")),
        ], tw))
        for w in rec.get("warnings", []) + rec.get("assumptions", []):
            story.append(ctx.p(f"• {w}", 8.5, color=(0.45, 0.3, 0)))

        # 7. Violations, eligibility, notes
        story += [PageBreak(), ctx.h(ctx.t("rpt.sec_violations"))]
        checks = results.get("eligibility", {}).get("checks", [])
        rows = []
        for chk in checks:
            if chk["status"] == "not_evaluated":
                rows.append([ctx.t(f"check.{chk['key']}"), ctx.t("check.not_evaluated"), "", "", chk.get("reason", "")])
            else:
                unit = chk.get("unit", "")
                unit_label = ctx.optional(f"unit.{unit}", unit)
                fmt = (lambda v: f"{ctx.area(v)} {unit_label}") if unit == "m²" else (lambda v: f"{v} {unit_label}")
                assumption = ctx.optional(f"assume.{chk['key']}", chk.get("assumption", "")) \
                    if chk.get("assumption") else ""
                rows.append([ctx.t(f"check.{chk['key']}"), ctx.t(f"check.{chk['status']}"), fmt(chk["value"]),
                             fmt(chk["limit"]), assumption])
        story.append(ctx.table([ctx.t("rpt.check"), ctx.t("rpt.result"), ctx.t("rpt.value"), ctx.t("rpt.limit"),
                                ctx.t("rpt.remarks")], rows or [["—", "", "", "", ""]],
                               [tw * 0.18, tw * 0.14, tw * 0.17, tw * 0.17, tw * 0.34]))
        disclaimer = results.get("eligibility", {}).get("disclaimer_ar" if ctx.rtl else "disclaimer_en", "")
        story += [Spacer(1, 2 * mm), ctx.p(disclaimer, 8.5, bold=True, color=(0.5, 0.1, 0.1))]
        issues = [i for i in results.get("issues", []) if i.get("message")]
        if issues:
            story += [Spacer(1, 4 * mm), ctx.h(ctx.t("rpt.issues"), 3)]
            for i in issues:
                story.append(ctx.p(f"• [{i['severity']}] {i['message']}", 8.2))
    story += [Spacer(1, 5 * mm), ctx.h(ctx.t("rpt.sec_notes"), 2)]
    if data.notes:
        for section, text in data.notes:
            story += [ctx.p(ctx.t(f"note.{section}"), 9, bold=True), ctx.p(text, 9, space_after=3)]
    else:
        story.append(ctx.p(ctx.t("rpt.no_notes"), 8.5, color=(0.4, 0.4, 0.4)))

    # Calculation trace ---------------------------------------------------------------------
    if results and opts.include_trace and results.get("trace"):
        story += [PageBreak(), ctx.h(ctx.t("rpt.sec_trace"))]
        rows = []
        for s in results["trace"]:
            inputs = json.dumps(s["inputs"], ensure_ascii=False, default=str)
            if len(inputs) > 220:
                inputs = inputs[:217] + "…"
            result = s["result"] if not isinstance(s["result"], float) else f"{s['result']:.4f}"
            rows.append([s["title"], s["formula"], inputs, f"{result} {s['unit']}",
                         "; ".join(x for x in s["sources"] + s["warnings"] if x)[:300]])
        story.append(ctx.table([ctx.t("rpt.step"), ctx.t("rpt.formula"), ctx.t("rpt.inputs"), ctx.t("rpt.result"),
                                ctx.t("rpt.sources_warnings")], rows,
                               [tw * 0.17, tw * 0.23, tw * 0.25, tw * 0.13, tw * 0.22]))

    # 8. Photographs ----------------------------------------------------------------------------
    if data.photos:
        story += [PageBreak(), ctx.h(ctx.t("rpt.sec_photos"))]
        for ph in data.photos:
            try:
                with Image.open(ph.path) as im:
                    iw, ih = im.size
                max_w, max_h = tw, th * 0.42
                ratio = min(max_w / iw, max_h / ih)
                story.append(KeepTogether([RLImage(str(ph.path), iw * ratio, ih * ratio),
                                           ctx.p(ph.caption or ph.path.name, 8.5, align="center", space_after=6)]))
            except (OSError, Image.UnidentifiedImageError) as exc:
                msg = ctx.t("rpt.photo_failed", name=ph.path.name, error=str(exc))
                warnings.append(msg)
                story.append(ctx.p(msg, color=(0.7, 0.1, 0.1)))

    # 9. Supporting documents manifest -----------------------------------------------------------
    story += [PageBreak(), ctx.h(ctx.t("rpt.sec_documents"))]
    if not manifest_entries:
        story.append(ctx.p(ctx.t("rpt.no_documents"), 8.5))
    else:
        rows = [[e["title"], e["reference"], e["file"], ", ".join(map(str, e["pages"])) or "—",
                 ctx.t("rpt.included") if e["status"] == "included" else f"{ctx.t('rpt.not_included')}: {e['error']}"]
                for e in manifest_entries]
        story.append(ctx.table([ctx.t("rpt.document"), ctx.t("rpt.reference"), ctx.t("rpt.file"),
                                ctx.t("rpt.pages"), ctx.t("rpt.result")], rows,
                               [tw * 0.22, tw * 0.15, tw * 0.23, tw * 0.12, tw * 0.28]))

    def on_page(c: rl_canvas.Canvas, _doc: Any) -> None:
        w, h = c._pagesize
        c.setFillColorRGB(0.35, 0.35, 0.35)
        header = opts.header_text or f"{opts.authority_name}  |  {data.case.get('case_number', '')}"
        draw_text(c, w - m if ctx.rtl else m, h - m + 2 * mm, header, 7.5, align="right" if ctx.rtl else "left",
                  rtl_default=ctx.rtl)
        c.setStrokeColorRGB(0.6, 0.6, 0.6)
        c.line(m, h - m, w - m, h - m)

    for pt in doc.pageTemplates:
        pt.onPage = on_page
    try:
        doc.build(story)
    except ReportError:
        raise
    return buf.getvalue()


# --------------------------------------------------------------------------- assembly


def _image_pdf(path: Path, title: str, ctx_lang: str) -> bytes:
    register_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4, invariant=1)
    w, h = A4
    with Image.open(path) as im:
        iw, ih = im.size
    ratio = min((w - 30 * mm) / iw, (h - 45 * mm) / ih)
    c.drawImage(str(path), (w - iw * ratio) / 2, (h - ih * ratio) / 2 - 5 * mm, iw * ratio, ih * ratio)
    draw_text(c, w / 2, h - 18 * mm, title, 10, align="center", bold=True, rtl_default=ctx_lang == "ar")
    c.showPage()
    c.save()
    return buf.getvalue()


def _stamp(writer: PdfWriter, opts: ReportOptions, footer: str) -> None:
    total = len(writer.pages)
    rgb = STATUS_COLORS[opts.status]
    for i, page in enumerate(writer.pages):
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=(w, h), invariant=1)
        c.setFillColorRGB(0.3, 0.3, 0.3)
        label = tr("rpt.page_of", opts.language, page=i + 1, total=total)
        draw_text(c, w / 2, 6 * mm, label, 7.5, align="center", rtl_default=opts.language == "ar")
        if footer:
            draw_text(c, w - 10 * mm if opts.language == "ar" else 10 * mm, 6 * mm, footer, 7,
                      align="right" if opts.language == "ar" else "left", rtl_default=opts.language == "ar")
        c.setFillColorRGB(*rgb)
        status = tr(f"status.{opts.status}", opts.language)
        draw_text(c, w - 10 * mm if opts.language != "ar" else 10 * mm, 6 * mm, status, 8,
                  align="right" if opts.language != "ar" else "left", bold=True, rtl_default=opts.language == "ar")
        if opts.status == "draft":
            c.saveState()
            c.setFillColorRGB(0.85, 0.2, 0.2)
            c.setFillAlpha(0.08)
            c.translate(w / 2, h / 2)
            c.rotate(35)
            draw_text(c, 0, 0, tr("rpt.draft_watermark", opts.language), 70, align="center", bold=True)
            c.restoreState()
        c.showPage()
        c.save()
        page.merge_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])


def build_report(data: ReportData, opts: ReportOptions, nf: NumberFormat | None = None) -> ReportResult:
    if opts.status not in STATUS_COLORS:
        raise ReportError(f"Invalid report status: {opts.status}")
    if not opts.generated_at:
        raise ReportError("generated_at must be given explicitly (reproducible output).")
    nf = nf or NumberFormat()
    warnings: list[str] = []
    entries = _preflight(data.documents)
    for e in entries:
        if e["status"] != "included":
            warnings.append(tr("rpt.doc_not_included", opts.language, name=e["file"], error=e["error"]))
    body = _body(data, opts, nf, entries, warnings)

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(body)).pages:
        writer.add_page(page)
    body_pages = len(writer.pages)
    for d, e in zip(data.documents, entries):
        if e["status"] != "included":
            continue
        e["first_page_in_report"] = len(writer.pages) + 1
        if e["type"] == "pdf":
            reader = PdfReader(str(d.path))
            for p in e["pages"]:
                writer.add_page(reader.pages[p - 1])
        else:
            for page in PdfReader(io.BytesIO(_image_pdf(d.path, d.title, opts.language))).pages:
                writer.add_page(page)
    _stamp(writer, opts, opts.footer_text)
    writer.add_metadata({"/Title": f"{opts.report_title or tr('rpt.title', opts.language)} "
                                   f"{data.case.get('case_number', '')}",
                         "/Producer": "Survey & Licensing Studio", "/Creator": "Survey & Licensing Studio",
                         "/CreationDate": "D:" + "".join(ch for ch in opts.generated_at if ch.isdigit())[:14],
                         "/ModDate": "D:" + "".join(ch for ch in opts.generated_at if ch.isdigit())[:14]})
    out = io.BytesIO()
    writer.write(out)
    pdf = out.getvalue()
    manifest = {
        "case_number": data.case.get("case_number", ""),
        "report_revision": opts.revision,
        "status": opts.status,
        "generated_at": opts.generated_at,
        "generated_by": opts.generated_by,
        "calculation": {k: v for k, v in (data.calculation or {}).items() if k != "results"},
        "rule_version": data.rule_version,
        "body_pages": body_pages,
        "total_pages": len(writer.pages),
        "photos": [{"file": p.path.name, "sha256": p.sha256, "caption": p.caption} for p in data.photos],
        "documents": entries,
        "warnings": warnings,
    }
    return ReportResult(pdf, manifest, warnings, len(writer.pages))
