"""Layer manager: real drawing layers with visibility, lock, colour, linetype, weight,
transparency, role/floor classification, ordering, search and active-layer selection.
All changes go through the undo stack.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..cad.commands import layer_command
from ..cad.model import LAYER_ROLES, Layer
from ..core.i18n import tr

LINETYPES = ("CONTINUOUS", "DASHED", "HIDDEN", "CENTER", "DOT")
COL_VIS, COL_LOCK, COL_COLOR, COL_NAME, COL_ROLE, COL_COUNT = range(6)


def swatch(color: str) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(QColor(color))
    return QIcon(pm)


class LayerPropertiesDialog(QDialog):
    def __init__(self, layer: Layer, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("layer.properties", name=layer.name))
        self.layer = Layer.from_dict(layer.to_dict())
        form = QFormLayout(self)
        self.color_btn = QPushButton(self.layer.color)
        self.color_btn.setIcon(swatch(self.layer.color))
        self.color_btn.clicked.connect(self._pick_color)
        self.linetype = QComboBox()
        self.linetype.addItems(LINETYPES if self.layer.linetype in LINETYPES else (*LINETYPES, self.layer.linetype))
        self.linetype.setCurrentText(self.layer.linetype)
        self.weight = QDoubleSpinBox()
        self.weight.setRange(0.0, 2.11)
        self.weight.setSingleStep(0.05)
        self.weight.setSuffix(" mm")
        self.weight.setValue(self.layer.lineweight)
        self.transp = QDoubleSpinBox()
        self.transp.setRange(0, 90)
        self.transp.setSuffix(" %")
        self.transp.setValue(self.layer.transparency * 100)
        self.role = QComboBox()
        for r in LAYER_ROLES:
            self.role.addItem(tr(f"role.{r}"), r)
        self.role.setCurrentIndex(LAYER_ROLES.index(self.layer.role) if self.layer.role in LAYER_ROLES else 0)
        self.floor = QLineEdit(self.layer.floor_key)
        self.floor.setPlaceholderText(tr("layer.floor_hint"))
        form.addRow(tr("layer.color"), self.color_btn)
        form.addRow(tr("layer.linetype"), self.linetype)
        form.addRow(tr("layer.lineweight"), self.weight)
        form.addRow(tr("layer.transparency"), self.transp)
        form.addRow(tr("layer.role"), self.role)
        form.addRow(tr("layer.floor"), self.floor)
        if self.layer.source_name:
            form.addRow(tr("layer.source_name"), QLineEdit(self.layer.source_name, readOnly=True))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(self.layer.color), self)
        if c.isValid():
            self.layer.color = c.name().upper()
            self.color_btn.setText(self.layer.color)
            self.color_btn.setIcon(swatch(self.layer.color))

    def result_layer(self) -> Layer:
        self.layer.linetype = self.linetype.currentText()
        self.layer.lineweight = round(self.weight.value(), 2)
        self.layer.transparency = round(self.transp.value() / 100, 2)
        self.layer.role = self.role.currentData()
        self.layer.floor_key = self.floor.text().strip()
        return self.layer


class LayerPanel(QWidget):
    layersChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.canvas = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("layer.search"))
        self.search.textChanged.connect(self.refresh)
        lay.addWidget(self.search)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([tr("layer.col.vis"), tr("layer.col.lock"), "", tr("layer.name"),
                                              tr("layer.role"), "#"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.table.horizontalHeader()
        for col in (COL_VIS, COL_LOCK, COL_COLOR, COL_COUNT):
            h.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        h.setSectionResizeMode(COL_ROLE, QHeaderView.Interactive)
        self.table.setColumnWidth(COL_ROLE, 120)
        self.setMinimumWidth(380)
        self.table.cellClicked.connect(self._clicked)
        self.table.cellDoubleClicked.connect(self._double_clicked)
        lay.addWidget(self.table)
        row1 = QHBoxLayout()
        row2 = QHBoxLayout()
        self.buttons = {}
        for key, slot, row in (("new", self.new_layer, row1), ("rename", self.rename_layer, row1),
                               ("duplicate", self.duplicate_layer, row1), ("delete", self.delete_layer, row1),
                               ("up", lambda: self.move(-1), row2), ("down", lambda: self.move(1), row2),
                               ("current", self.set_current, row2), ("props", self.edit_properties, row2)):
            b = QPushButton(tr(f"layer.btn.{key}"))
            b.clicked.connect(slot)
            row.addWidget(b)
            self.buttons[key] = b
        lay.addLayout(row1)
        lay.addLayout(row2)

    def set_canvas(self, canvas) -> None:  # type: ignore[no-untyped-def]
        self.canvas = canvas
        self.refresh()

    @property
    def drawing(self):  # type: ignore[no-untyped-def]
        return self.canvas.drawing if self.canvas else None

    def refresh(self) -> None:
        d = self.drawing
        self.table.setRowCount(0)
        if d is None:
            return
        counts: dict[str, int] = {}
        for e in d.entities.values():
            counts[e.layer] = counts.get(e.layer, 0) + 1
        needle = self.search.text().strip().casefold()
        for layer in d.layers:
            role_label = tr(f"role.{layer.role}")
            if needle and needle not in layer.name.casefold() and needle not in role_label.casefold():
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            vis = QTableWidgetItem("●" if layer.visible else "○")
            lock = QTableWidgetItem("■" if layer.locked else "□")
            color = QTableWidgetItem()
            color.setIcon(swatch(layer.color))
            name = QTableWidgetItem(("▶ " if layer.name == d.active_layer else "") + layer.name +
                                    (f"  [{layer.floor_key}]" if layer.floor_key else ""))
            name.setData(Qt.UserRole, layer.name)
            if layer.derived:
                name.setForeground(QColor("#BA68C8"))
                name.setToolTip(tr("layer.derived_tip"))
            role = QTableWidgetItem(role_label)
            cnt = QTableWidgetItem(str(counts.get(layer.name, 0)))
            for col, item in enumerate((vis, lock, color, name, role, cnt)):
                item.setData(Qt.UserRole, layer.name)
                self.table.setItem(r, col, item)
            if layer.name == d.active_layer:
                f = name.font()
                f.setBold(True)
                name.setFont(f)

    def selected_name(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), COL_NAME)
        return item.data(Qt.UserRole) if item else None

    def _apply(self, fn, label: str) -> None:  # type: ignore[no-untyped-def]
        if self.drawing is None:
            return
        if not self.canvas.can_edit():
            QMessageBox.warning(self, tr("app.title"), tr("tool.read_only"))
            return
        try:
            cmd = layer_command(self.drawing, fn, label)
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, tr("app.title"), str(exc))
            return
        self.canvas.stack.push(cmd, already_applied=True)
        self.canvas.invalidate()
        self.canvas.edited()
        self.refresh()
        self.layersChanged.emit()

    def _clicked(self, row: int, col: int) -> None:
        name = self.table.item(row, col).data(Qt.UserRole)
        if col == COL_VIS:
            self._apply(lambda d: setattr(d.layer(name), "visible", not d.layer(name).visible), "layer visibility")
        elif col == COL_LOCK:
            self._apply(lambda d: setattr(d.layer(name), "locked", not d.layer(name).locked), "layer lock")
        elif col == COL_COLOR:
            c = QColorDialog.getColor(QColor(self.drawing.layer(name).color), self)
            if c.isValid():
                self._apply(lambda d: setattr(d.layer(name), "color", c.name().upper()), "layer colour")

    def _double_clicked(self, row: int, col: int) -> None:
        if col in (COL_NAME, COL_ROLE, COL_COUNT):
            name = self.table.item(row, col).data(Qt.UserRole)
            self._apply(lambda d: setattr(d, "active_layer", name), "current layer")

    def new_layer(self) -> None:
        name, ok = QInputDialog.getText(self, tr("layer.btn.new"), tr("layer.name"))
        if ok and name.strip():
            self._apply(lambda d: d.add_layer(Layer(name.strip())), "new layer")

    def rename_layer(self) -> None:
        old = self.selected_name()
        if not old:
            return
        new, ok = QInputDialog.getText(self, tr("layer.btn.rename"), tr("layer.name"), text=old)
        if ok and new.strip() and new != old:
            self._apply(lambda d: d.rename_layer(old, new.strip()), "rename layer")

    def duplicate_layer(self) -> None:
        name = self.selected_name()
        if name:
            self._apply(lambda d: d.duplicate_layer(name), "duplicate layer")

    def delete_layer(self) -> None:
        name = self.selected_name()
        if name and QMessageBox.question(self, tr("layer.btn.delete"), tr("layer.confirm_delete", name=name)) \
                == QMessageBox.Yes:
            self._apply(lambda d: d.remove_layer(name), "delete layer")

    def move(self, delta: int) -> None:
        name = self.selected_name()
        if not name:
            return
        idx = [layer.name for layer in self.drawing.layers].index(name)
        self._apply(lambda d: d.move_layer(name, idx + delta), "reorder layer")
        for r in range(self.table.rowCount()):
            if self.table.item(r, COL_NAME).data(Qt.UserRole) == name:
                self.table.selectRow(r)

    def set_current(self) -> None:
        name = self.selected_name()
        if name:
            self._apply(lambda d: setattr(d, "active_layer", name), "current layer")

    def edit_properties(self) -> None:
        name = self.selected_name()
        if not name:
            return
        dlg = LayerPropertiesDialog(self.drawing.layer(name), self)
        if dlg.exec() == QDialog.Accepted:
            new = dlg.result_layer()

            def apply(d):  # type: ignore[no-untyped-def]
                layer = d.layer(name)
                for field in ("color", "linetype", "lineweight", "transparency", "role", "floor_key"):
                    setattr(layer, field, getattr(new, field))

            self._apply(apply, "layer properties")
