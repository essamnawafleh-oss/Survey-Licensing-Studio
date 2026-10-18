"""Reviewer-entered setback values for a case whose rule text is conditional."""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from ...core.i18n import current_language, tr
from ...persistence.workspace import Workspace
from .common import show_error


class OverrideDialog(QDialog):
    def __init__(self, ws: Workspace, case_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws, self.case_id = ws, case_id
        self.setWindowTitle(tr("ovr.title"))
        self.resize(640, 480)
        lay = QVBoxLayout(self)
        case = ws.cases.get(case_id)
        self.version_id, self.code = case.rule_version_id, case.land_use_code
        if not self.version_id or not self.code:
            lay.addWidget(QLabel(tr("case.rule_none")))
            return
        try:
            self.rule = ws.rules.rule(self.version_id, self.code)
        except Exception as exc:  # noqa: BLE001 - shown verbatim
            lay.addWidget(QLabel(str(exc)))
            return
        note = QLabel(tr("ovr.note"))
        note.setWordWrap(True)
        note.setObjectName("warning")
        lay.addWidget(note)
        lay.addWidget(QLabel(f"<b>{self.rule.label(current_language())}</b>"))
        if self.rule.notes:
            r = QLabel(self.rule.notes)
            r.setWordWrap(True)
            lay.addWidget(r)
        form = QFormLayout()
        current = ws.rules.case_override(case_id, self.version_id, self.code)
        self.boxes: dict[str, QDoubleSpinBox] = {}
        types = list(self.rule.setbacks_raw) or ["front", "side", "rear"]
        for btype in types:
            raw = self.rule.raw_setback(btype)
            label = (tr("btype." + btype) if btype in ("front", "side", "rear") else btype) + f"  —  «{raw or '—'}»"
            if btype in self.rule.setbacks:
                form.addRow(label, QLabel(tr("ovr.fixed", v=f"{self.rule.setbacks[btype]:g}")))
                continue
            box = QDoubleSpinBox()
            box.setRange(-1, 1000)
            box.setDecimals(2)
            box.setSuffix(" m")
            box.setSpecialValueText(tr("ovr.not_set"))
            box.setValue(current.setbacks.get(btype, -1) if current else -1)
            form.addRow(label, box)
            self.boxes[btype] = box
        lay.addLayout(form)
        lay.addWidget(QLabel(tr("ovr.justification")))
        self.text = QPlainTextEdit(current.note if current else "")
        lay.addWidget(self.text)
        if current:
            lay.addWidget(QLabel(tr("ovr.current", by=current.entered_by, at=current.entered_at)))
        row = QHBoxLayout()
        save = QPushButton(tr("ovr.save"))
        save.clicked.connect(self._save)
        save.setEnabled(ws.user.can("verify_rules") and bool(self.boxes))
        clear = QPushButton(tr("ovr.clear"))
        clear.clicked.connect(self._clear)
        clear.setEnabled(ws.user.can("verify_rules") and current is not None)
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.reject)
        row.addStretch(1)
        for b in (save, clear, close):
            row.addWidget(b)
        lay.addLayout(row)

    def _save(self) -> None:
        values = {k: b.value() for k, b in self.boxes.items() if b.value() >= 0}
        try:
            self.ws.rules.set_case_override(self.ws.user, self.case_id, self.version_id, self.code, values,
                                            self.text.toPlainText())
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        QMessageBox.information(self, tr("ovr.title"), tr("ovr.saved"))
        self.accept()

    def _clear(self) -> None:
        try:
            self.ws.rules.clear_case_override(self.ws.user, self.case_id)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.accept()
