"""Right sidebar: entity properties (editable) and the case/licensing inspector."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QHeaderView, QLabel, QLineEdit,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout,
                               QWidget)

from ..cad import ops
from ..cad.commands import ReplaceEntities
from ..core.i18n import tr


class PropertiesWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.canvas = None
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(4, 4, 4, 4)
        self.header = QLabel(tr("props.none"))
        self.header.setWordWrap(True)
        self.lay.addWidget(self.header)
        self.form = QFormLayout()
        self.lay.addLayout(self.form)
        self.coords = QTableWidget(0, 2)
        self.coords.setHorizontalHeaderLabels(["X", "Y"])
        self.coords.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.coords.itemChanged.connect(self._coord_edited)
        self.lay.addWidget(self.coords)
        self.lay.addStretch(1)
        self._loading = False
        self._entity_id: str | None = None

    def set_canvas(self, canvas) -> None:  # type: ignore[no-untyped-def]
        self.canvas = canvas

    def _clear_form(self) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)

    def refresh(self) -> None:
        self._loading = True
        self._clear_form()
        self.coords.setRowCount(0)
        self.coords.setVisible(False)
        self._entity_id = None
        c = self.canvas
        d = c.drawing if c else None
        sel = [d.entities[i] for i in c.selection if d and i in d.entities] if c else []
        if not sel:
            self.header.setText(tr("props.none"))
            self._loading = False
            return
        fmt_l, fmt_a = self._formatters()
        if len(sel) > 1:
            self.header.setText(tr("props.multi", n=len(sel)))
            total_len = sum(ops.entity_length(e) or 0 for e in sel)
            areas = [ops.entity_area(e) for e in sel]
            self.form.addRow(tr("props.total_length"), QLabel(fmt_l(total_len)))
            self.form.addRow(tr("props.total_area"), QLabel(fmt_a(sum(a for a in areas if a))))
            self.form.addRow("", QLabel(tr("props.area_sum_note")))
            self._loading = False
            return
        e = sel[0]
        self._entity_id = e.id
        layer = d.layer(e.layer)
        self.header.setText(f"<b>{tr('kind_entity.' + e.kind)}</b>  <span style='color:#8C96A3'>{e.id[:8]}</span>")
        layer_combo = QComboBox()
        layer_combo.addItems([lay.name for lay in d.layers])
        layer_combo.setCurrentText(e.layer)
        layer_combo.currentTextChanged.connect(lambda name: self._edit(lambda n: setattr(n, "layer", name)))
        self.form.addRow(tr("props.layer"), layer_combo)
        self.form.addRow(tr("props.role"), QLabel(tr(f"role.{layer.role}")))
        source = e.meta.get("source", "survey")
        src_label = QLabel(tr(f"source.{source}") if source in ("survey", "regulatory", "derived", "user") else source)
        src_label.setObjectName("warning" if source == "derived" else "muted")
        self.form.addRow(tr("props.source"), src_label)
        if e.meta.get("approximated_from"):
            w = QLabel(tr("props.approximated", src=e.meta["approximated_from"]))
            w.setObjectName("warning")
            w.setWordWrap(True)
            self.form.addRow("", w)
        if e.meta.get("block"):
            self.form.addRow(tr("props.block"), QLabel(str(e.meta["block"])))
        if "elevation" in e.meta:
            self.form.addRow(tr("props.elevation"), QLabel(str(e.meta["elevation"])))
        length = ops.entity_length(e)
        area = ops.entity_area(e)
        if length is not None:
            self.form.addRow(tr("props.length"), QLabel(fmt_l(length)))
        if area is not None:
            self.form.addRow(tr("props.area"), QLabel(fmt_a(area)))
        if layer.role == "building" and e.kind in ("polyline", "circle"):
            floor = QLineEdit(str(e.meta.get("floor", "")))
            floor.setPlaceholderText(tr("props.floor_from_layer", floor=layer.floor_key or "—"))
            floor.editingFinished.connect(lambda: self._edit_meta("floor", floor.text().strip()))
            self.form.addRow(tr("props.floor"), floor)
        note = QLineEdit(str(e.meta.get("classification_note", "")))
        note.editingFinished.connect(lambda: self._edit_meta("classification_note", note.text().strip()))
        self.form.addRow(tr("props.classification_note"), note)
        if e.kind == "text":
            text = QLineEdit(e.data.get("text", ""))
            text.editingFinished.connect(lambda: self._edit(lambda n: n.data.__setitem__("text", text.text())))
            self.form.addRow(tr("props.text"), text)
            h = QDoubleSpinBox()
            h.setRange(0.001, 1e6)
            h.setDecimals(3)
            h.setValue(e.data.get("height", 1.0))
            h.editingFinished.connect(lambda: self._edit(lambda n: n.data.__setitem__("height", h.value())))
            self.form.addRow(tr("props.height"), h)
        if e.kind in ("circle", "arc"):
            r = QDoubleSpinBox()
            r.setRange(1e-6, 1e9)
            r.setDecimals(4)
            r.setValue(e.data["radius"])
            r.editingFinished.connect(lambda: self._edit(lambda n: n.data.__setitem__("radius", r.value())))
            self.form.addRow(tr("props.radius"), r)
        if e.kind == "polyline":
            closed = QComboBox()
            closed.addItems([tr("props.open"), tr("props.closed")])
            closed.setCurrentIndex(1 if e.data.get("closed") else 0)
            closed.currentIndexChanged.connect(lambda i: self._edit(lambda n: n.data.__setitem__("closed", bool(i))))
            self.form.addRow(tr("props.closure"), closed)
        verts = e.vertices()
        if verts:
            self.coords.setVisible(True)
            self.coords.setRowCount(len(verts))
            for i, (x, y) in enumerate(verts):
                self.coords.setItem(i, 0, QTableWidgetItem(f"{x:.4f}"))
                self.coords.setItem(i, 1, QTableWidgetItem(f"{y:.4f}"))
        self._loading = False

    def _formatters(self):  # type: ignore[no-untyped-def]
        f = self.canvas._unit_factor() if self.canvas else None
        if f:
            return (lambda v: f"{v * f:,.3f} m"), (lambda v: f"{v * f * f:,.3f} m²")
        u = tr("tool.drawing_units")
        return (lambda v: f"{v:,.4f} {u}"), (lambda v: f"{v:,.4f} {u}² ({tr('props.units_unverified')})")

    def _edit(self, fn) -> None:  # type: ignore[no-untyped-def]
        if self._loading or self._entity_id is None:
            return
        c = self.canvas
        e = c.drawing.entities.get(self._entity_id)
        if e is None:
            return
        if c.drawing.layer(e.layer).locked or not c.can_edit():
            self.refresh()
            return
        n = e.clone()
        fn(n)
        c.stack.push(ReplaceEntities([e.clone()], [n], "properties"))
        c.edited()

    def _edit_meta(self, key: str, value: str) -> None:
        def fn(n):  # type: ignore[no-untyped-def]
            if value:
                n.meta[key] = value
            else:
                n.meta.pop(key, None)
        e = self.canvas.drawing.entities.get(self._entity_id or "")
        if e is not None and str(e.meta.get(key, "")) != value:
            self._edit(fn)

    def _coord_edited(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        try:
            value = float(item.text())
        except ValueError:
            self.refresh()
            return
        row, col = item.row(), item.column()

        def fn(n):  # type: ignore[no-untyped-def]
            d = n.data
            if n.kind in ("line", "dimension"):
                d["p1" if row == 0 else "p2"][col] = value
            elif n.kind == "polyline":
                d["points"][row][col] = value
            elif n.kind in ("circle", "arc"):
                d["center"][col] = value
            else:
                d["position"][col] = value
        self._edit(fn)


class Inspector(QTabWidget):
    openCase = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.props = PropertiesWidget()
        self.addTab(self.props, tr("inspector.properties"))
        self.case_view = QTextBrowser()
        self.case_view.setOpenLinks(False)
        self.addTab(self.case_view, tr("inspector.licensing"))

    def show_case_summary(self, html: str) -> None:
        self.case_view.setHtml(html)


class CommandLine(QWidget):
    submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        self.log = QTextBrowser()
        self.log.setMaximumHeight(110)
        self.log.setStyleSheet("font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 9pt;")
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(tr("cmd.placeholder"))
        self.edit.returnPressed.connect(self._submit)
        lay.addWidget(self.log)
        lay.addWidget(self.edit)
        self.history: list[str] = []
        self._hist_idx = 0
        self.edit.installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:  # type: ignore[no-untyped-def]
        from PySide6.QtCore import QEvent

        if obj is self.edit and ev.type() == QEvent.KeyPress and self.history:
            if ev.key() == Qt.Key_Up:
                self._hist_idx = max(0, self._hist_idx - 1)
                self.edit.setText(self.history[self._hist_idx])
                return True
            if ev.key() == Qt.Key_Down:
                self._hist_idx = min(len(self.history), self._hist_idx + 1)
                self.edit.setText(self.history[self._hist_idx] if self._hist_idx < len(self.history) else "")
                return True
        return super().eventFilter(obj, ev)

    def _submit(self) -> None:
        text = self.edit.text()
        self.edit.clear()
        if text.strip():
            self.history.append(text)
        self._hist_idx = len(self.history)
        self.append(f"<span style='color:#8C96A3'>&gt; {text}</span>")
        self.submitted.emit(text)

    def append(self, html: str) -> None:
        self.log.append(html)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def message(self, text: str, level: str = "info") -> None:
        color = {"info": "#D7DDE4", "warn": "#E0A040", "error": "#E05A50", "ok": "#5CB85C"}[level]
        from html import escape

        self.append(f"<span style='color:{color}'>{escape(text)}</span>")

