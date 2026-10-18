"""Administrator editor for the configurable case form fields."""
from __future__ import annotations

import re

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QLineEdit, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from ...core.i18n import tr
from ...persistence.cases import CaseFieldDef
from ...persistence.workspace import Workspace
from .common import fill_table, make_table, show_error

_KEY = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
TYPES = ("text", "multiline", "date", "number", "choice")


class FieldsDialog(QDialog):
    def __init__(self, ws: Workspace, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws = ws
        self.setWindowTitle(tr("fields.title"))
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        self.table = make_table([tr("fields.key"), tr("fields.label_ar"), tr("fields.label_en"), tr("fields.type"),
                                 tr("fields.required"), tr("fields.sensitive")])
        self.table.currentCellChanged.connect(lambda r, *_: self._load(r))
        lay.addWidget(self.table)
        f = QFormLayout()
        self.key = QLineEdit()
        self.label_ar = QLineEdit()
        self.label_en = QLineEdit()
        self.type = QComboBox()
        self.type.addItems(TYPES)
        self.choices = QLineEdit()
        self.choices.setPlaceholderText(tr("fields.choices_hint"))
        self.required = QCheckBox()
        self.sensitive = QCheckBox()
        self.order = QSpinBox()
        self.order.setRange(0, 999)
        for label, w in ((tr("fields.key"), self.key), (tr("fields.label_ar"), self.label_ar),
                         (tr("fields.label_en"), self.label_en), (tr("fields.type"), self.type),
                         (tr("fields.choices"), self.choices), (tr("fields.required"), self.required),
                         (tr("fields.sensitive"), self.sensitive), (tr("fields.order"), self.order)):
            f.addRow(label, w)
        lay.addLayout(f)
        save = QPushButton(tr("fields.save"))
        save.clicked.connect(self._save)
        lay.addWidget(save)
        self._refresh()

    def _refresh(self) -> None:
        self.defs = self.ws.cases.field_defs()
        fill_table(self.table, [[d.key, d.label_ar, d.label_en, d.field_type, tr("common.yes") if d.required else "",
                                 tr("common.yes") if d.sensitive else ""] for d in self.defs])

    def _load(self, row: int) -> None:
        if 0 <= row < len(self.defs):
            d = self.defs[row]
            self.key.setText(d.key)
            self.label_ar.setText(d.label_ar)
            self.label_en.setText(d.label_en)
            self.type.setCurrentText(d.field_type)
            self.choices.setText(", ".join(d.choices))
            self.required.setChecked(d.required)
            self.sensitive.setChecked(d.sensitive)
            self.order.setValue(d.sort_order)

    def _save(self) -> None:
        key = self.key.text().strip()
        if not _KEY.match(key) or not self.label_ar.text().strip() or not self.label_en.text().strip():
            show_error(self, ValueError(tr("fields.invalid")))
            return
        choices = [c.strip() for c in self.choices.text().split(",") if c.strip()]
        try:
            self.ws.cases.upsert_field_def(self.ws.user, CaseFieldDef(
                key, self.label_ar.text().strip(), self.label_en.text().strip(), self.type.currentText(), choices,
                self.required.isChecked(), self.sensitive.isChecked(), self.order.value()))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self._refresh()
