"""Regulatory plan transformation from user-entered control points (never from visual alignment)."""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ...cad.transform import fit
from ...core.i18n import tr
from ...services.case_service import CaseService
from .common import make_table, show_error


class TransformDialog(QDialog):
    def __init__(self, service: CaseService, drawing_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service, self.drawing_id = service, drawing_id
        self.applied = False
        self.setWindowTitle(tr("tf.title"))
        self.resize(820, 520)
        lay = QVBoxLayout(self)
        note = QLabel(tr("tf.note"))
        note.setWordWrap(True)
        note.setObjectName("warning")
        lay.addWidget(note)
        self.table = make_table([tr("tf.src_x"), tr("tf.src_y"), tr("tf.dst_x"), tr("tf.dst_y"), tr("tf.residual")],
                                editable=True)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        add = QPushButton("+")
        add.clicked.connect(lambda: self.table.insertRow(self.table.rowCount()))
        rem = QPushButton("−")
        rem.clicked.connect(lambda: self.table.removeRow(self.table.currentRow()))
        self.kind = QComboBox()
        self.kind.addItem(tr("tf.kind.translation"), "translation")
        self.kind.addItem(tr("tf.kind.similarity"), "similarity")
        compute = QPushButton(tr("tf.compute"))
        compute.clicked.connect(self._compute)
        self.apply_btn = QPushButton(tr("tf.apply"))
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply)
        for w in (add, rem, self.kind, compute, self.apply_btn):
            row.addWidget(w)
        lay.addLayout(row)
        self.result = QLabel()
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        for _ in range(3):
            self.table.insertRow(self.table.rowCount())

    def _pairs(self) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        src, dst = [], []
        for r in range(self.table.rowCount()):
            vals = [(self.table.item(r, c).text().strip() if self.table.item(r, c) else "") for c in range(4)]
            if not any(vals):
                continue
            try:
                x, y, u, v = (float(t) for t in vals)
            except ValueError as exc:
                raise ValueError(tr("tf.bad_row", row=r + 1)) from exc
            src.append((x, y))
            dst.append((u, v))
        return src, dst

    def _compute(self) -> None:
        self.apply_btn.setEnabled(False)
        try:
            src, dst = self._pairs()
            res = fit(src, dst, self.kind.currentData())
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        rows = [r for r in range(self.table.rowCount())
                if any(self.table.item(r, c) and self.table.item(r, c).text().strip() for c in range(4))]
        for r, resid in zip(rows, res.residuals):
            self.table.setItem(r, 4, QTableWidgetItem(f"{resid:.4f}"))
        t = res.transform
        self.result.setText(tr("tf.result", tx=f"{t.tx:,.4f}", ty=f"{t.ty:,.4f}", rot=f"{t.rotation_deg:.6f}",
                               scale=f"{t.scale:.8f}", rms=f"{res.rms:.4f}", n=len(src)))
        self.apply_btn.setEnabled(True)

    def _apply(self) -> None:
        if QMessageBox.question(self, tr("tf.title"), tr("tf.confirm")) != QMessageBox.Yes:
            return
        try:
            src, dst = self._pairs()
            loaded = self.service.apply_transformation(self.drawing_id, src, dst, self.kind.currentData())
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.applied = True
        QMessageBox.information(self, tr("tf.title"), tr("tf.applied", rev=loaded.revision.revision))
        self.accept()
