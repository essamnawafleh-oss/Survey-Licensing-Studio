"""Assemble report data from a case and record report exports."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

from ..core.i18n import tr
from ..persistence.reports_repo import ReportExport
from ..persistence.workspace import Workspace
from ..reporting.report import PhotoItem, ReportData, ReportOptions, ReportResult, SourceDocument, build_report
from .case_service import CaseService


class ReportService:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self.cases = CaseService(ws)

    def approval_blockers(self, case_id: str, status: str) -> list[str]:
        """Conditions that must hold before a report can be marked reviewed/approved."""
        if status == "draft":
            return []
        blockers: list[str] = []
        case = self.ws.cases.get(case_id)
        calc = self.ws.calculations.latest(case_id)
        if calc is None:
            blockers.append("No calculation has been run.")
        elif calc.results.get("status") != "ok":
            blockers.append(f"The latest calculation status is '{calc.results.get('status')}', not 'ok'.")
        if not case.units.verified:
            blockers.append("Drawing units are not verified.")
        if case.rule_version_id and not self.ws.rules.get_version(case.rule_version_id).verified:
            blockers.append("The applied rule version has not been verified by a reviewer.")
        unverified = [lic.license_number for lic in self.ws.licenses.list(case_id)
                      if lic.verification_status == "unverified"]
        if unverified:
            blockers.append("Unverified license records: " + ", ".join(unverified))
        if calc is not None and calc.results.get("status") == "ok":
            # content, not the drawing revision id: re-saving unchanged geometry is not a change
            if self.cases.build_inputs(case_id).content_key() != calc.inputs.content_key():
                blockers.append("The case changed after the latest calculation; recalculate first.")
        return blockers

    def collect(self, case_id: str, lang: str, include_documents: bool = True) -> ReportData:
        ws = self.ws
        case = ws.cases.get(case_id)
        defs = ws.cases.field_defs()
        rv = ws.rules.get_version(case.rule_version_id) if case.rule_version_id else None
        rule_label = ""
        if rv is not None:
            try:
                r = ws.rules.rule(rv.id, case.land_use_code)
                rule_label = r.label(lang)
            except Exception as exc:  # noqa: BLE001 - message shown in the report
                rule_label = f"{case.land_use_code} ({exc})"
        calc = ws.calculations.latest(case_id)
        loaded = self.cases.survey_drawing(case_id)
        drawing = loaded.drawing if loaded else None
        unit_to_m = None
        try:
            unit_to_m = case.units.require_metric_planar()
        except Exception:  # noqa: BLE001 - absence is reported in the PDF ("no drawing with verified units")
            unit_to_m = None
        if drawing is not None and calc is not None:
            self.cases.apply_derived_geometry(drawing, calc.results, calc.id)

        licenses = []
        docs: list[SourceDocument] = []
        for lic in ws.licenses.list(case_id):
            d = asdict(lic)
            d["issues"] = lic.completeness_issues()
            licenses.append(d)
            if include_documents:
                for doc in lic.documents:
                    if doc.include_in_report:
                        fr = ws.files.get(doc.file_id)
                        docs.append(SourceDocument("license_doc", f"{tr('rpt.license_number', lang)} "
                                                                  f"{lic.license_number}", ws.store.absolute(fr.stored_path),
                                                   fr.sha256, doc.page_selection, lic.license_number,
                                                   fr.original_name))
        if include_documents:
            for att in ws.media.attachments(case_id):
                if att.include_in_report:
                    fr = ws.files.get(att.file_id)
                    docs.append(SourceDocument("attachment", att.title, ws.store.absolute(fr.stored_path), fr.sha256,
                                               att.page_selection, "", fr.original_name))
        photos = []
        for ph in ws.media.photos(case_id):
            if ph.include_in_report:
                fr = ws.files.get(ph.file_id)
                photos.append(PhotoItem(ws.store.absolute(fr.stored_path), ph.caption, fr.sha256))
        status_label = tr(f"case_status.{case.status}", lang)
        return ReportData(
            case={"case_number": case.case_number, "parcel_identifier": case.parcel_identifier,
                  "status_label": status_label, "survey_date": case.survey_date, "surveyor": case.surveyor,
                  "land_use": rule_label or case.land_use_code, "units": case.units.unit.value
                  + ("" if case.units.verified else " (unverified)"), "crs": case.units.crs_id or "—"},
            custom_fields=[(fd.label(lang), case.fields.get(fd.key, ""), fd.sensitive) for fd in defs],
            rule_version=({"id": rv.id, "label": rv.label, "sha256": rv.workbook_sha256, "verified": rv.verified,
                           "sheet": rv.sheet_name} if rv else None),
            calculation=({"id": calc.id, "revision": calc.revision, "input_hash": calc.input_hash,
                          "result_hash": calc.result_hash, "results": calc.results} if calc else None),
            licenses=licenses,
            notes=[(n.section, n.text) for n in ws.media.notes(case_id)],
            drawing=drawing, unit_to_m=unit_to_m, photos=photos, documents=docs,
        )

    def preview(self, case_id: str, options: ReportOptions) -> ReportResult:
        if not options.generated_at:
            options.generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        options.generated_by = options.generated_by or self.ws.user.display_name
        options.revision = self.ws.reports.next_revision(case_id)
        data = self.collect(case_id, options.language)
        return build_report(data, options, self.ws.settings.number_format)

    def export(self, case_id: str, options: ReportOptions) -> tuple[ReportExport, ReportResult]:
        blockers = self.approval_blockers(case_id, options.status)
        if blockers:
            from ..core.errors import ValidationError

            raise ValidationError("The report cannot be marked " + options.status + ": " + " ".join(blockers),
                                  blockers=blockers)
        result = self.preview(case_id, options)
        case = self.ws.cases.get(case_id)
        calc = self.ws.calculations.latest(case_id)
        name = f"{case.case_number}_report_r{options.revision:03d}_{options.status}.pdf"
        export = self.ws.reports.save(self.ws.user, case_id, options.revision, options.status,
                                      calc.id if calc else None, result.pdf, name, result.manifest,
                                      options.to_dict(), options.generated_at)
        manifest_bytes = json.dumps(result.manifest, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        self.ws.files.add_bytes(self.ws.user, manifest_bytes, name.replace(".pdf", "_manifest.json"), case_id, "report")
        return export, result
