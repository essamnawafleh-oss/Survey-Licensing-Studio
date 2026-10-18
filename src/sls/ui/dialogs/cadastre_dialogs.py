"""Parcel register: import a GIS parcel layer (Shapefile) and find a parcel by its identifiers."""
from __future__ import annotations


from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ...core.i18n import tr
from ...persistence.workspace import Workspace
from .common import fill_table, make_table


from .gis_data_dialogs import CadastreImportDialog  # noqa: E402,F401  (staged import; kept for callers)


class FindParcelDialog(QDialog):
    """Cascading search: village → basin → sector → parcel number, or the DLS key."""

    def __init__(self, ws: Workspace, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ws = ws
        self.selected_ids: list[int] = []
        self.results = []
        self.setWindowTitle(tr("cad.find_title"))
        self.resize(980, 560)
        lay = QVBoxLayout(self)
        layer = ws.cadastre.active_layer()
        head = QLabel(tr("cad.using", label=layer.label) if layer else tr("cad.none"))
        head.setObjectName("muted" if layer else "error")
        lay.addWidget(head)
        form = QHBoxLayout()
        self.village, self.basin, self.sector = QComboBox(), QComboBox(), QComboBox()
        self.parcel = QLineEdit()
        self.parcel.setPlaceholderText(tr("cad.parcel_hint"))
        self.key = QLineEdit()
        self.key.setPlaceholderText(tr("cad.key_hint"))
        for label, w in ((tr("cad.field.village"), self.village), (tr("cad.field.basin"), self.basin),
                         (tr("cad.field.sector"), self.sector), (tr("cad.field.parcel"), self.parcel)):
            form.addWidget(QLabel(label))
            form.addWidget(w)
        lay.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("cad.field.key")))
        row.addWidget(self.key, 1)
        search = QPushButton(tr("cad.search"))
        search.setDefault(True)
        search.clicked.connect(self._search)
        row.addWidget(search)
        lay.addLayout(row)
        self.table = make_table([tr("cad.field.key"), tr("cad.field.village"), tr("cad.field.basin"),
                                 tr("cad.field.sector"), tr("cad.field.sheet"), tr("cad.field.parcel"),
                                 tr("cad.area"), tr("cad.parts"), tr("zon.zones"), tr("cad.issue")])
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.doubleClicked.connect(self.accept)
        lay.addWidget(self.table, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setObjectName("warning")
        lay.addWidget(self.note)
        btns = QHBoxLayout()
        use = QPushButton(tr("cad.use"))
        use.clicked.connect(self.accept)
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(use)
        btns.addWidget(close)
        lay.addLayout(btns)
        self.village.currentIndexChanged.connect(self._load_basins)
        self.basin.currentIndexChanged.connect(self._load_sectors)
        self._fill(self.village, ws.cadastre.distinct("village"))

    @staticmethod
    def _fill(cb: QComboBox, values: list[str]) -> None:
        cb.blockSignals(True)
        cb.clear()
        cb.addItem("", "")
        for v in values:
            if v:
                cb.addItem(v, v)
        cb.blockSignals(False)

    def _load_basins(self) -> None:
        v = self.village.currentData() or ""
        self._fill(self.basin, self.ws.cadastre.distinct("basin", village=v) if v else [])
        self._load_sectors()

    def _load_sectors(self) -> None:
        v, b = self.village.currentData() or "", self.basin.currentData() or ""
        self._fill(self.sector, self.ws.cadastre.distinct("sector", village=v, basin=b) if v and b else [])

    def _search(self) -> None:
        if not (self.key.text().strip() or self.parcel.text().strip()):
            QMessageBox.information(self, tr("cad.find_title"), tr("cad.need_criteria"))
            return
        self.results = self.ws.cadastre.find(self.village.currentData() or "", self.basin.currentData() or "",
                                             self.sector.currentData() or "", self.parcel.text(), self.key.text())
        fill_table(self.table, [[p.dls_key, p.village, p.basin, p.sector, p.sheet, p.parcel_no,
                                 f"{p.area_m2:,.2f}" if p.area_m2 is not None else "—", p.parts, self._zones(p), p.issue]
                                for p in self.results])
        keys = {p.dls_key for p in self.results}
        if not self.results:
            self.note.setText(tr("cad.no_results"))
        elif len(self.results) > len(keys):
            self.note.setText(tr("cad.repeated_note", n=len(self.results)))
        else:
            self.note.setText("")
        if len(self.results) == 1:
            self.table.selectRow(0)

    def _zones(self, p) -> str:  # type: ignore[no-untyped-def]
        if p.geometry is None:
            return ""
        ov = self.ws.zoning.overlay(p.geometry)
        if ov is None:
            return ""
        return zones_text(ov)

    def accept(self) -> None:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        if not rows:
            return
        self.selected_ids = [self.results[r].id for r in rows]
        super().accept()



def zones_text(ov) -> str:  # type: ignore[no-untyped-def]
    parts = [f"{z.zone_id} {z.zone_name} ({z.pct:.0f}%)" for z in ov.zones]
    if ov.unzoned:
        parts.append(tr("zon.unzoned", area=f"{sum(z.area_m2 for z in ov.unzoned):.1f}"))
    if ov.uncovered_m2 >= 1.0:
        parts.append(tr("zon.gap", area=f"{ov.uncovered_m2:.1f}"))
    return " + ".join(parts) or tr("zon.none")
