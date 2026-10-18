"""Drawing import preview: entity accounting, warnings, units suggestion and layer-role mapping."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QLabel, QLineEdit,
                               QListWidget, QSplitter, QTableWidgetItem, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from ...cad.dxf_io import ImportReport
from ...cad.model import LAYER_ROLES, Drawing
from ...core.i18n import tr
from .common import fill_table, make_table, ok_cancel


class ImportPreviewDialog(QDialog):
    def __init__(self, drawing: Drawing, report: ImportReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.drawing = drawing
        self.report = report
        self.setWindowTitle(tr("import.title", name=report.source_name))
        self.resize(900, 640)
        lay = QVBoxLayout(self)
        info = QFormLayout()
        info.addRow(tr("import.version"), QLabel(report.dxf_version))
        unit_lbl = QLabel(tr(f"unit_name.{report.suggested_unit}") + f"  ($INSUNITS = {report.insunits_code})")
        unit_lbl.setObjectName("warning")
        info.addRow(tr("import.units"), unit_lbl)
        info.addRow("", QLabel(tr("import.units_note")))
        if report.extents:
            x0, y0, x1, y1 = report.extents
            info.addRow(tr("import.extents"), QLabel(f"X {x0:,.3f} … {x1:,.3f}   Y {y0:,.3f} … {y1:,.3f}"))
        if report.crs_message:
            crs_lbl = QLabel(("✓ " if report.crs_ok else "⚠ ") + report.crs_message)
            crs_lbl.setWordWrap(True)
            crs_lbl.setObjectName("ok" if report.crs_ok else "error")
            info.addRow(tr("import.crs_check"), crs_lbl)
        if report.converter:
            info.addRow(tr("import.converter"), QLabel(report.converter))
        lay.addLayout(info)
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        counts = make_table([tr("import.type"), tr("import.imported"), tr("import.approximated"),
                             tr("import.expanded"), tr("import.skipped")])
        types = sorted(set(report.imported) | set(report.approximated) | set(report.expanded) | set(report.skipped))
        fill_table(counts, [[t, report.imported.get(t, ""), report.approximated.get(t, ""),
                             report.expanded.get(t, ""), report.skipped.get(t, "")] for t in types])
        ll.addWidget(QLabel(tr("import.accounting")))
        ll.addWidget(counts)
        if report.actions:
            ll.addWidget(QLabel(tr("import.actions", n=len(report.actions))))
            acts = QListWidget()
            acts.setWordWrap(True)
            for a in report.actions:
                acts.addItem("→ " + a)
            ll.addWidget(acts)
        warn = QListWidget()
        for w in report.warnings + [f"[audit] {f}" for f in report.audit_fixes] + \
                [f"[converter] {m}" for m in report.converter_messages if m.upper().startswith("ERROR")]:
            warn.addItem(w)
        ll.addWidget(QLabel(tr("import.warnings", n=len(report.warnings))))
        ll.addWidget(warn)
        split.addWidget(left)
        right = QGroupBox(tr("import.layer_mapping"))
        rl = QVBoxLayout(right)
        self.layers = make_table([tr("layer.name"), "#", tr("layer.role"), tr("layer.floor")], editable=True)
        self.role_boxes: list[tuple[str, QComboBox, QLineEdit]] = []
        for name, count in report.layers.items():
            r = self.layers.rowCount()
            self.layers.insertRow(r)
            self.layers.setItem(r, 0, QTableWidgetItem(name))
            self.layers.setItem(r, 1, QTableWidgetItem(str(count)))
            combo = QComboBox()
            for role in LAYER_ROLES:
                combo.addItem(tr(f"role.{role}"), role)
            current = drawing.layer(name).role
            combo.setCurrentIndex(LAYER_ROLES.index(current) if current in LAYER_ROLES else 0)
            floor = QLineEdit(drawing.layer(name).floor_key)
            floor.setPlaceholderText("G, 1, 2 …")
            self.layers.setCellWidget(r, 2, combo)
            self.layers.setCellWidget(r, 3, floor)
            self.role_boxes.append((name, combo, floor))
        rl.addWidget(QLabel(tr("import.mapping_note")))
        rl.addWidget(self.layers)
        split.addWidget(right)
        split.setSizes([420, 480])
        lay.addWidget(split, 1)
        opts = QFormLayout()
        self.role = QComboBox()
        self.role.addItem(tr("import.role_survey"), "survey")
        self.role.addItem(tr("import.role_regulatory"), "regulatory")
        self.keep = QCheckBox(tr("import.keep_copy"))
        self.keep.setChecked(True)
        opts.addRow(tr("import.drawing_role"), self.role)
        opts.addRow("", self.keep)
        lay.addLayout(opts)
        lay.addWidget(ok_cancel(self))

    def apply_mapping(self) -> None:
        for name, combo, floor in self.role_boxes:
            layer = self.drawing.layer(name)
            layer.role = combo.currentData()
            layer.floor_key = floor.text().strip()
