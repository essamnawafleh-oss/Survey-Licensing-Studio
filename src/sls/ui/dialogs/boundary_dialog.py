"""Property boundary selection, per-edge classification (user-confirmed) and verification."""
from __future__ import annotations

import math

from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
                               QWidget)

from shapely.geometry import LineString

from ...cad.model import Drawing, flatten_bulge_polyline, logical_boundary
from ...core.i18n import tr
from ...persistence.drawings_repo import ParcelRecord
from ...persistence.workspace import Workspace
from .common import make_table, show_error


class BoundaryDialog(QDialog):
    def __init__(self, ws: Workspace, case_id: str, drawing: Drawing, selected_ids: set[str],
                 extra_types: list[str], suggestions: list[str | None] | None, canvas=None,  # type: ignore[no-untyped-def]
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws, self.case_id, self.drawing, self.canvas = ws, case_id, drawing, canvas
        self.parcel: ParcelRecord = ws.drawings.parcel(case_id)
        self.setWindowTitle(tr("boundary.title"))
        self.resize(640, 520)
        lay = QVBoxLayout(self)
        self.info = QLabel()
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        use_sel = QPushButton(tr("boundary.use_selection"))
        use_sel.clicked.connect(lambda: self._use_selection(selected_ids))
        lay.addWidget(use_sel)
        self.table = make_table([tr("rpt.edge"), tr("rpt.edge_length_m"), tr("rpt.boundary_type"),
                                 tr("boundary.suggestion")])
        lay.addWidget(self.table, 1)
        self.types = ["front", "side", "rear"] + [t for t in extra_types if t not in ("front", "side", "rear")]
        self.suggestions = suggestions or []
        note = QLabel(tr("boundary.note"))
        note.setWordWrap(True)
        note.setObjectName("warning")
        lay.addWidget(note)
        row = QHBoxLayout()
        save = QPushButton(tr("boundary.save"))
        save.clicked.connect(lambda: self._save(False))
        verify = QPushButton(tr("boundary.verify"))
        verify.clicked.connect(lambda: self._save(True))
        verify.setEnabled(ws.user.can("verify_boundary"))
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(save)
        row.addWidget(verify)
        row.addWidget(close)
        lay.addLayout(row)
        self._load()

    def _boundary(self):  # type: ignore[no-untyped-def]
        return self.drawing.entities.get(self.parcel.boundary_entity_id) if self.parcel.boundary_entity_id else None

    def _use_selection(self, ids: set[str]) -> None:
        ents = [self.drawing.entities[i] for i in ids if i in self.drawing.entities]
        if len(ents) != 1 or ents[0].kind != "polyline" or not ents[0].data.get("closed"):
            QMessageBox.warning(self, tr("boundary.title"), tr("boundary.need_closed"))
            return
        if self.parcel.boundary_entity_id != ents[0].id:
            self.parcel.boundary_entity_id = ents[0].id
            self.parcel.edge_types = []
            self.parcel.verified = False
        self._load()

    def _load(self) -> None:
        b = self._boundary()
        self.table.setRowCount(0)
        if b is None:
            self.info.setText(tr("boundary.none"))
            return
        from ...engine.inputs import _tolerance

        case = self.ws.cases.get(self.case_id)
        # the same logical edges the calculation uses (duplicate/closing vertices merged; one row per arc)
        norm = logical_boundary(b.data["points"], bool(b.data.get("closed")), _tolerance(case.units))
        pts = [(p[0], p[1]) for p in norm]
        if any(abs(p[2]) > 1e-12 for p in norm):
            self.info.setText(tr("boundary.has_arcs"))
        current = [[p[0], p[1]] for p in b.data["points"]]
        state = tr("boundary.verified", user=self.parcel.verified_by, at=self.parcel.verified_at) \
            if self.parcel.verified_against(current) else (tr("boundary.changed") if self.parcel.verified
                                                          else tr("boundary.not_verified"))
        self.info.setText(f"{tr('boundary.entity')}: {b.id[:8]} — {len(pts)} {tr('boundary.vertices')} — {state}")
        f = self.canvas._unit_factor() if self.canvas else None
        labels = []
        for i in range(len(pts)):
            a, c = pts[i], pts[(i + 1) % len(pts)]
            length = math.dist(a, c) if abs(norm[i][2]) < 1e-12 else \
                LineString(flatten_bulge_polyline([norm[i], c], False, 128)).length  # arc length
            r = self.table.rowCount()
            self.table.insertRow(r)
            from PySide6.QtWidgets import QTableWidgetItem

            self.table.setItem(r, 0, QTableWidgetItem(str(i + 1)))
            self.table.setItem(r, 1, QTableWidgetItem(f"{length * f:.3f}" if f else f"{length:.4f} ({tr('tool.drawing_units')})"))
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItem("")
            for t in self.types:
                combo.addItem(t)
            if i < len(self.parcel.edge_types):
                combo.setCurrentText(self.parcel.edge_types[i])
            self.table.setCellWidget(r, 2, combo)
            sug = self.suggestions[i] if i < len(self.suggestions) else None
            self.table.setItem(r, 3, QTableWidgetItem(sug or "—"))
            labels.append(((a[0] + c[0]) / 2, (a[1] + c[1]) / 2, str(i + 1)))
        if self.canvas is not None:
            self.canvas.annotations = labels
            self.canvas.highlight_ids = {b.id}
            self.canvas.update()

    def _save(self, verify: bool) -> None:
        b = self._boundary()
        if b is None:
            QMessageBox.warning(self, tr("boundary.title"), tr("boundary.none"))
            return
        types = [self.table.cellWidget(r, 2).currentText().strip() for r in range(self.table.rowCount())]
        if verify and any(not t for t in types):
            QMessageBox.warning(self, tr("boundary.title"), tr("boundary.unclassified"))
            return
        new_points = [[p[0], p[1]] for p in b.data["points"]]
        if types != self.parcel.edge_types or new_points != self.parcel.boundary_points:
            self.parcel.verified = False
        self.parcel.edge_types = types
        self.parcel.boundary_points = new_points
        try:
            self.parcel = self.ws.drawings.save_parcel(self.ws.user, self.parcel, verify=verify)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self._load()

    def done(self, r: int) -> None:
        if self.canvas is not None:
            self.canvas.annotations = []
            self.canvas.highlight_ids = set()
            self.canvas.update()
        super().done(r)
