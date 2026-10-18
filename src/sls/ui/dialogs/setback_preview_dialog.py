"""CAD-style preview of the setback offset construction, with reviewer acceptance.

Read-only until *Accept*: cancelling changes nothing. Accepting stores a fingerprinted approval
and writes the construction as derived layers in a new drawing revision (CaseService).
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSplitter,
                               QTabWidget, QVBoxLayout, QWidget)
from shapely import wkt as shapely_wkt

from ...core.i18n import tr
from ...geometry.primitives import geom_parts
from ...persistence.workspace import Workspace
from ...services.case_service import CaseService
from .common import fill_table, make_table, show_error

STEPS = (  # key, colour, default visibility
    ("parcel", "#FFFFFF", True), ("offset", "#4FC3F7", True), ("joined", "#80DEEA", False),
    ("trimmed", "#EF5350", True), ("extended", "#FFD54F", True), ("corner", "#F06292", True),
    ("discarded", "#9E9E9E", True), ("buildable", "#66BB6A", True),
)


class ConstructionView(QWidget):
    """Y-up view of the construction (drawing units). Wheel zooms, drag pans."""

    def __init__(self, parcel_wkt: str, con: dict[str, Any] | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 360)
        self.parcel = shapely_wkt.loads(parcel_wkt) if parcel_wkt else None
        self.con = con or {}
        self.visible = {k: v for k, _, v in STEPS}
        self.highlight_edges: set[int] = set()
        self.highlight_vertices: set[int] = set()
        self.discarded = [(shapely_wkt.loads(f["wkt"]), f["reason"]) for f in self.con.get("discarded", []) if f["wkt"]]
        self.buildable = shapely_wkt.loads(self.con["buildable_wkt"]) if self.con.get("buildable_wkt") else None
        self._scale = 1.0
        self._center = QPointF(0, 0)
        self._drag: QPointF | None = None
        self.fit()

    def fit(self) -> None:
        geoms = [g for g in (self.parcel, self.buildable) if g is not None and not g.is_empty]
        pts = [p for e in self.con.get("edges", []) for p in e["raw_offset"] + e["trimmed_extended"]]
        xs = [p[0] for p in pts] + [b for g in geoms for b in (g.bounds[0], g.bounds[2])]
        ys = [p[1] for p in pts] + [b for g in geoms for b in (g.bounds[1], g.bounds[3])]
        if not xs:
            return
        w, h = max(max(xs) - min(xs), 1e-6), max(max(ys) - min(ys), 1e-6)
        self._center = QPointF((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
        self._scale = 0.9 * min(max(self.width(), 420) / w, max(self.height(), 360) / h)
        self.update()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        super().resizeEvent(event)
        self.fit()

    def _s(self, x: float, y: float) -> QPointF:
        return QPointF(self.width() / 2 + (x - self._center.x()) * self._scale,
                       self.height() / 2 - (y - self._center.y()) * self._scale)

    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self._scale *= 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.update()

    def mousePressEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self._drag = event.position()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        if self._drag is not None:
            d = event.position() - self._drag
            self._center -= QPointF(d.x() / self._scale, -d.y() / self._scale)
            self._drag = event.position()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self._drag = None

    def _poly_path(self, geom) -> QPainterPath:  # type: ignore[no-untyped-def]
        path = QPainterPath()
        for p in geom_parts(geom):
            for ring in [p.exterior, *p.interiors]:
                cs = list(ring.coords)
                path.moveTo(self._s(*cs[0]))
                for c in cs[1:]:
                    path.lineTo(self._s(*c))
                path.closeSubpath()
        return path

    def _line(self, qp: QPainter, seg: list[list[float]], color: str, width: float = 1.4,
              style: Qt.PenStyle = Qt.SolidLine) -> None:
        qp.setPen(QPen(QColor(color), width, style))
        qp.drawLine(self._s(*seg[0]), self._s(*seg[1]))

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        qp.fillRect(self.rect(), QColor("#1E1E1E"))
        col = {k: c for k, c, _ in STEPS}
        if self.visible["buildable"] and self.buildable is not None and not self.buildable.is_empty:
            qp.setPen(QPen(QColor(col["buildable"]), 2))
            qp.setBrush(QBrush(QColor(102, 187, 106, 60)))
            qp.drawPath(self._poly_path(self.buildable))
        if self.visible["discarded"]:
            qp.setPen(QPen(QColor(col["discarded"]), 1, Qt.DashLine))
            qp.setBrush(QBrush(QColor(158, 158, 158, 70), Qt.BDiagPattern))
            for g, _ in self.discarded:
                qp.drawPath(self._poly_path(g))
        qp.setBrush(Qt.NoBrush)
        if self.visible["parcel"] and self.parcel is not None:
            qp.setPen(QPen(QColor(col["parcel"]), 1.6))
            qp.drawPath(self._poly_path(self.parcel))
        for e in self.con.get("edges", []):
            hi = e["index"] in self.highlight_edges
            if self.visible["offset"]:
                self._line(qp, e["raw_offset"], "#E040FB" if hi else col["offset"], 3 if hi else 1.2, Qt.DashLine)
            if self.visible["joined"] and not e["collapsed"]:
                self._line(qp, e["trimmed_extended"], "#E040FB" if hi else col["joined"], 3 if hi else 1.6)
            if self.visible["trimmed"]:
                for seg in e["trimmed"]:
                    self._line(qp, seg, col["trimmed"], 2.4)
            if self.visible["extended"]:
                for seg in e["extended"]:
                    self._line(qp, seg, col["extended"], 2.4)
        if self.visible["corner"]:
            for j in self.con.get("joins", []):
                hi = j["vertex"] in self.highlight_vertices
                qp.setPen(QPen(QColor("#E040FB" if hi else col["corner"]), 3 if hi else 1.5))
                for q in j["points"]:
                    c = self._s(*q)
                    r = 7 if hi else 4
                    qp.drawLine(QPointF(c.x() - r, c.y()), QPointF(c.x() + r, c.y()))
                    qp.drawLine(QPointF(c.x(), c.y() - r), QPointF(c.x(), c.y() + r))
        qp.setPen(QColor("#CCCCCC"))
        y = 16
        for k, c, _ in STEPS:
            if self.visible[k]:
                qp.fillRect(QRectF(8, y - 9, 10, 10), QColor(c))
                qp.drawText(QPointF(24, y), tr(f"sbp.layer.{k}"))
                y += 16
        qp.end()


class SetbackPreviewDialog(QDialog):
    def __init__(self, ws: Workspace, case_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws, self.case_id = ws, case_id
        self.service = CaseService(ws)
        self.approval: dict[str, Any] | None = None
        self.setWindowTitle(tr("sbp.title"))
        self.resize(1180, 720)
        lay = QVBoxLayout(self)
        self.preview = self.service.setback_preview(case_id)
        con = self.con = self.preview["construction"]
        parcel_wkt = self.preview["parcel_wkt"]

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.view = ConstructionView(parcel_wkt, con)
        ll.addWidget(self.view, 1)
        toggles = QHBoxLayout()
        for k, _, on in STEPS:
            cb = QCheckBox(tr(f"sbp.layer.{k}"))
            cb.setChecked(on)
            cb.toggled.connect(lambda v, key=k: self._toggle(key, v))
            toggles.addWidget(cb)
        fit = QPushButton(tr("act.zoom_extents"))
        fit.clicked.connect(self.view.fit)
        toggles.addWidget(fit)
        ll.addLayout(toggles)
        split.addWidget(left)

        tabs = QTabWidget()
        self.issues = make_table([tr("sbp.severity"), tr("sbp.code"), tr("sbp.edges"), tr("sbp.vertices"),
                                  tr("sbp.area"), tr("sbp.message")])
        rows = []
        self._issue_rows = (con or {}).get("issues", [])
        for i in self._issue_rows:
            rows.append([tr(f"sbp.sev.{i['severity']}"), i["code"], ", ".join(map(str, i["edges"])),
                         ", ".join(map(str, i["vertices"])), "" if i["area_m2"] is None else f"{i['area_m2']:.2f}",
                         i["message"]])
        for e in self.preview["errors"]:
            rows.append([tr("sbp.sev.error"), e.get("code", ""), "", "", "", f"[{e['section']}] {e['message']}"])
        fill_table(self.issues, rows)
        self.issues.itemSelectionChanged.connect(self._highlight)
        tabs.addTab(self.issues, tr("sbp.tab.issues", n=len(rows)))
        edges = make_table([tr("rpt.edge"), tr("rpt.boundary_type"), tr("rpt.setback_m"), tr("sbp.rule_version"),
                            tr("sbp.start"), tr("sbp.end"), tr("sbp.outcome"), tr("sbp.used_m")])
        rv = self.preview.get("rule_version_id") or ""
        fill_table(edges, [[e["index"], e["boundary_type"], f"{e['setback_m']:g}", rv[:8],
                            tr(f"sbp.action.{e['start_action']}"), tr(f"sbp.action.{e['end_action']}"),
                            tr(f"sbp.outcome.{e['outcome']}"), f"{e['used_length_m']:.3f}"]
                           for e in (con or {}).get("edges", [])])
        tabs.addTab(edges, tr("sbp.tab.edges"))
        split.addWidget(tabs)
        split.setSizes([700, 480])
        lay.addWidget(split, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText(tr("sbp.note_placeholder"))
        self.note.setMaximumHeight(64)
        lay.addWidget(self.note)
        self.reviewed = QCheckBox(tr("sbp.reviewed"))
        lay.addWidget(self.reviewed)
        row = QHBoxLayout()
        self.accept_btn = QPushButton(tr("sbp.accept"))
        self.accept_btn.clicked.connect(self._accept)
        cancel = QPushButton(tr("common.cancel"))
        cancel.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(self.accept_btn)
        row.addWidget(cancel)
        lay.addLayout(row)
        self.note.textChanged.connect(self._update_state)
        self.reviewed.toggled.connect(self._update_state)
        self._update_state()

    def _toggle(self, key: str, on: bool) -> None:
        self.view.visible[key] = on
        self.view.update()

    def _highlight(self) -> None:
        rows = {i.row() for i in self.issues.selectedIndexes()}
        sel = [self._issue_rows[r] for r in rows if r < len(self._issue_rows)]
        self.view.highlight_edges = {e for i in sel for e in i["edges"]}
        self.view.highlight_vertices = {v for i in sel for v in i["vertices"]}
        self.view.update()

    def state(self) -> str:
        con = self.con
        if con is None:
            return "blocked"
        if any(i["severity"] == "error" for i in con["issues"]):
            return "invalid"
        return "review" if con["review_keys"] else "clean"

    def _update_state(self) -> None:
        st = self.state()
        con = self.con or {}
        lines = [tr(f"sbp.state.{st}", n=len(con.get("review_keys", [])))]
        appr = self.preview.get("approval")
        if appr and con:
            changed = [k for k, v in con["fingerprint_parts"].items() if appr["fingerprint_parts"].get(k) != v]
            if changed:
                lines.append(tr("sbp.approval_stale", by=appr["approved_by"], at=appr["approved_at"],
                                what=", ".join(tr(f"sbp.fp.{k}") for k in changed)))
            else:
                lines.append(tr("sbp.approval_valid", by=appr["approved_by"], at=appr["approved_at"]))
        if st == "review" and not self.ws.user.can("approve_setback"):
            lines.append(tr("sbp.need_reviewer"))
        self.status.setText("\n".join(lines))
        needs_review = st == "review"
        self.note.setEnabled(st in ("clean", "review"))
        self.reviewed.setVisible(needs_review)
        ok = st == "clean" or (needs_review and self.ws.user.can("approve_setback") and self.reviewed.isChecked()
                               and bool(self.note.toPlainText().strip()))
        self.accept_btn.setEnabled(ok)

    def _accept(self) -> None:
        try:
            self.approval = self.service.approve_setback(self.case_id, self.note.toPlainText(),
                                                         self.con["fingerprint"])  # type: ignore[index]
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.accept()
