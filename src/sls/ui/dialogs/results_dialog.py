"""Calculation results: five separate area categories, tables, eligibility checks, issues and the full trace."""
from __future__ import annotations

import json
from html import escape

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTabWidget, QTextBrowser,
                               QVBoxLayout, QWidget)

from ...core.i18n import current_language, tr
from ...core.settings import NumberFormat
from ...persistence.calculations_repo import CalculationRecord
from ...persistence.workspace import Workspace
from .common import fill_table, make_table


def summary_html(calc: CalculationRecord, nf: NumberFormat) -> str:
    r = calc.results
    t = r.get("totals", {})

    def a(v):  # type: ignore[no-untyped-def]
        return "—" if v is None else f"{nf.area(v)} m²"

    status_col = {"ok": "#5CB85C", "partial": "#E0A040", "error": "#E05A50"}.get(r.get("status"), "#ccc")
    rows = [(tr("cat.measured"), a(t.get("measured_total_area_m2"))),
            (tr("cat.licensed"), a(t.get("previously_licensed_area_m2"))),
            (tr("cat.remaining"), a(t.get("remaining_to_assess_m2")) if t.get("remaining_to_assess_m2") is not None
             else "— " + escape(r.get("reconciliation", {}).get("total_remaining_reason", ""))),
            (tr("cat.violation"), a(t.get("setback_violation_area_m2"))),
            (tr("cat.eligible"), tr("rpt.eligible_not_determined"))]
    html = [f"<p><b>{tr('calc.revision')} #{calc.revision}</b> — <span style='color:{status_col}'>"
            f"{tr('calc.status.' + r.get('status', 'error'))}</span><br><small>{calc.created_at} · {calc.created_by}"
            f" · {tr('rpt.input_hash')} {calc.input_hash[:12]}</small></p><table cellpadding=4>"]
    html += [f"<tr><td>{escape(k)}</td><td align=right><b>{v}</b></td></tr>" for k, v in rows]
    html.append("</table>")
    if t.get("outside_parcel_area_m2"):
        html.append(f"<p style='color:#E0A040'>{tr('rpt.outside_parcel')}: {a(t.get('outside_parcel_area_m2'))}</p>")
    for e in r.get("errors", []):
        html.append(f"<p style='color:#E05A50'>■ [{escape(e['section'])}] {escape(e['message'])}</p>")
    key = "disclaimer_ar" if current_language() == "ar" else "disclaimer_en"
    html.append(f"<p><small>{escape(r.get('eligibility', {}).get(key, ''))}</small></p>")
    return "".join(html)


class ResultsDialog(QDialog):
    applyGeometry = Signal(str)

    def __init__(self, ws: Workspace, calc: CalculationRecord, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws, self.calc = ws, calc
        nf = ws.settings.number_format
        r = calc.results
        self.setWindowTitle(tr("calc.title", rev=calc.revision))
        self.resize(980, 640)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        summary = QTextBrowser()
        summary.setHtml(summary_html(calc, nf))
        tabs.addTab(summary, tr("calc.tab.summary"))

        floors = make_table([tr("rpt.floor"), tr("rpt.measured_area"), tr("calc.sum_parts"), tr("rpt.overlap_removed"),
                             tr("rpt.in_setback"), tr("rpt.outside_parcel"), tr("calc.parts"), tr("calc.entities")])
        fill_table(floors, [[f["floor_key"], nf.area(f["area_m2"]), nf.area(f["sum_of_parts_m2"]),
                             nf.area(f["overlap_m2"]), _opt(nf, f.get("in_setback_zone_m2")),
                             _opt(nf, f.get("outside_parcel_m2")), f["part_count"], len(f["entity_ids"])]
                            for f in r.get("floors", [])])
        tabs.addTab(floors, tr("calc.tab.floors"))

        sb = r.get("setback")
        sbw = QWidget()
        sl = QVBoxLayout(sbw)
        if sb:
            sl.addWidget(QLabel(f"{tr('rpt.buildable_area')}: {nf.area(sb['buildable_area_m2'])} m² — "
                                f"{tr('setback.' + sb['status'])}"))
            edges = make_table([tr("rpt.edge"), tr("rpt.boundary_type"), tr("rpt.edge_length_m"), tr("rpt.setback_m")])
            fill_table(edges, [[e["index"], e["boundary_type"], nf.length(e["length_m"]), nf.length(e["setback_m"])]
                               for e in sb["edges"]])
            sl.addWidget(edges)
            for w in sb.get("warnings", []):
                lbl = QLabel(w)
                lbl.setObjectName("warning")
                lbl.setWordWrap(True)
                sl.addWidget(lbl)
        else:
            lbl = QLabel(tr("calc.no_setback"))
            lbl.setObjectName("error")
            sl.addWidget(lbl)
        tabs.addTab(sbw, tr("calc.tab.setbacks"))

        rec = r.get("reconciliation", {})
        lic = make_table([tr("calc.item"), tr("calc.measured"), tr("calc.licensed"), tr("calc.remaining"),
                          tr("calc.licenses"), tr("rpt.remarks")])
        rows = []
        for line in rec.get("areas", []) + rec.get("lengths", []):
            rows.append([line["key"], _q(line["measured"], line["unit"]), _q(line["licensed"], line["unit"]),
                         _q(line["remaining"], line["unit"]), ", ".join(line["license_numbers"]),
                         "; ".join(line["conflicts"] + line["warnings"])])
        fill_table(lic, rows)
        lw = QWidget()
        ll = QVBoxLayout(lw)
        ll.addWidget(lic)
        for w in rec.get("warnings", []) + rec.get("assumptions", []):
            lbl = QLabel("• " + w)
            lbl.setWordWrap(True)
            lbl.setObjectName("warning")
            ll.addWidget(lbl)
        tabs.addTab(lw, tr("calc.tab.licenses"))

        el = make_table([tr("rpt.check"), tr("rpt.result"), tr("rpt.value"), tr("rpt.limit"), tr("calc.excess"),
                         tr("rpt.remarks")])
        fill_table(el, [[tr(f"check.{c['key']}"), tr(f"check.{c['status']}"), c.get("value", ""), c.get("limit", ""),
                         c.get("excess", ""), c.get("reason") or c.get("assumption", "")]
                        for c in r.get("eligibility", {}).get("checks", [])])
        tabs.addTab(el, tr("calc.tab.eligibility"))

        issues = make_table([tr("calc.severity"), tr("calc.code"), tr("calc.message"), tr("calc.entity")])
        fill_table(issues, [[i["severity"], i["code"], i["message"], i.get("entity_id", "")[:8]]
                            for i in r.get("issues", [])])
        tabs.addTab(issues, tr("calc.tab.issues", n=len(r.get("issues", []))))

        trace = make_table([tr("rpt.step"), tr("rpt.formula"), tr("rpt.inputs"), tr("rpt.result"), tr("calc.unit"),
                            tr("rpt.sources_warnings")])
        fill_table(trace, [[s["title"], s["formula"], json.dumps(s["inputs"], ensure_ascii=False, default=str),
                            s["result"], s["unit"], "; ".join(x for x in s["sources"] + s["warnings"] if x)]
                           for s in r.get("trace", [])])
        tabs.addTab(trace, tr("calc.tab.trace"))

        row = QHBoxLayout()
        apply_btn = QPushButton(tr("calc.apply_geometry"))
        apply_btn.setToolTip(tr("calc.apply_geometry_tip"))
        apply_btn.clicked.connect(lambda: self.applyGeometry.emit(calc.id))
        repro = QPushButton(tr("calc.reproduce"))
        repro.clicked.connect(self._reproduce)
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.accept)
        row.addWidget(apply_btn)
        row.addWidget(repro)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)

    def _reproduce(self) -> None:
        ok, h = self.ws.calculations.reproduce(self.calc.id)
        if ok:
            QMessageBox.information(self, tr("calc.reproduce"), tr("calc.reproduced", h=h[:16]))
        else:
            QMessageBox.warning(self, tr("calc.reproduce"), tr("calc.not_reproduced", h=h[:16]))


def _opt(nf: NumberFormat, v: float | None) -> str:
    return "—" if v is None else nf.area(v)


def _q(v: float | None, unit: str) -> str:
    return "—" if v is None else f"{v:,.2f} {unit}"
