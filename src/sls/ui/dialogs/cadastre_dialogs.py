"""Parcel register: import a GIS parcel layer (Shapefile) and find a parcel by its identifiers."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ...core.i18n import tr
from ...gis.shapefile_layer import (LOGICAL_FIELDS, ZONING_FIELDS, FieldMapping, inspect, iter_records, suggest_mapping,
                                   suggest_zoning_mapping, summarize)
from ...persistence.workspace import Workspace
from .common import fill_table, make_table, show_error


class CadastreImportDialog(QDialog):
    """Import a parcel layer (kind='cadastre') or a land-use zoning layer (kind='zoning')."""

    def __init__(self, ws: Workspace, run_busy, parent: QWidget | None = None,  # type: ignore[no-untyped-def]
                 kind: str = "cadastre") -> None:
        super().__init__(parent)
        self.ws, self.run_busy, self.kind = ws, run_busy, kind
        self.path: Path | None = None
        self.info = None
        self.setWindowTitle(tr("cad.import_title") if kind == "cadastre" else tr("zon.import_title"))
        self.resize(900, 640)
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.file = QLineEdit()
        self.file.setReadOnly(True)
        browse = QPushButton(tr("rules.browse"))
        browse.clicked.connect(self._browse)
        row.addWidget(QLabel(tr("cad.layer_file")))
        row.addWidget(self.file, 1)
        row.addWidget(browse)
        lay.addLayout(row)
        self.meta = QLabel()
        self.meta.setWordWrap(True)
        lay.addWidget(self.meta)
        self.preview = make_table([])
        self.preview.setMaximumHeight(200)
        lay.addWidget(self.preview)
        g = QGroupBox(tr("cad.mapping"))
        f = QFormLayout(g)
        self.boxes: dict[str, QComboBox] = {}
        for fld in (LOGICAL_FIELDS if kind == "cadastre" else ZONING_FIELDS):
            cb = QComboBox()
            self.boxes[fld] = cb
            f.addRow(tr(f"cad.field.{fld}") if fld in LOGICAL_FIELDS else tr(f"zon.field.{fld}"), cb)
        lay.addWidget(g)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        row2 = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("cad.name_hint"))
        check = QPushButton(tr("cad.check"))
        check.clicked.connect(self._check)
        self.import_btn = QPushButton(tr("cad.import"))
        self.import_btn.setEnabled(False)
        self.import_btn.clicked.connect(self._import)
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.reject)
        row2.addWidget(QLabel(tr("cad.name")))
        row2.addWidget(self.name, 1)
        for b in (check, self.import_btn, close):
            row2.addWidget(b)
        lay.addLayout(row2)
        if kind == "cadastre":
            current = ws.cadastre.active_layer()
            if current is not None:
                self.meta.setText(tr("cad.current", label=current.label, at=current.imported_at, by=current.imported_by))
        elif (zl := ws.zoning.active_layer()) is not None:
            self.meta.setText(tr("cad.current", label=zl["label"], at=zl["imported_at"], by=""))

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("cad.layer_file"), "", "Shapefile (*.shp)")
        if not path:
            return
        try:
            self.info = inspect(Path(path))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.path = Path(path)
        self.file.setText(path)
        self.name.setText(self.name.text() or self.path.stem)
        i = self.info
        crs = i.crs_code or tr("cad.crs_unknown")
        missing = (" — " + tr("cad.missing", files=", ".join(i.missing_components))) if i.missing_components else ""
        self.meta.setText(tr("cad.meta", type=i.shape_type, n=i.count, crs=crs, enc=i.encoding) + missing)
        import shapefile

        with shapefile.Reader(str(self.path), encoding=i.encoding) as r:
            rows = [list(rec) for rec in r.records()[:20]]
        self.preview.setColumnCount(len(i.fields))
        self.preview.setHorizontalHeaderLabels(i.fields)
        fill_table(self.preview, rows)
        suggested = (suggest_mapping(i.fields) if self.kind == "cadastre" else suggest_zoning_mapping(i.fields)).columns
        for fld, cb in self.boxes.items():
            cb.clear()
            cb.addItem("", "")
            for name in i.fields:
                cb.addItem(name, name)
            cb.setCurrentText(suggested.get(fld, ""))
        self.import_btn.setEnabled(False)
        self.summary.setText("")

    def _mapping(self) -> FieldMapping:
        return FieldMapping({k: cb.currentData() for k, cb in self.boxes.items() if cb.currentData()})

    def _check(self) -> None:
        if self.path is None:
            return
        m = self._mapping()
        try:
            records = self.run_busy(tr("cad.reading"), lambda: list(iter_records(self.path, self.info.encoding)))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        if self.kind == "zoning":
            lines = self._zoning_summary(records, m)
            self.summary.setText("\n".join(lines))
            self.import_btn.setEnabled(self.ws.user.can("import_zoning") and bool(m.columns.get("zone_id")))
            return
        s = summarize(records, m)
        lines = [tr("cad.summary", **{k: s[k] if s[k] is not None else "—" for k in (
            "records", "invalid", "multipart", "missing_key", "repeated_keys", "repeated_key_records", "missing_parcel")})]
        bad = [r for r in records if not r.valid][:10]
        lines += [f"  • #{r.fid}: {r.issue}" for r in bad]
        from ...core.crs import same_grid

        if self.info.crs_code and not same_grid(self.info.crs_code, self.ws.settings.default_crs):
            lines.append(tr("cad.crs_differs", crs=self.info.crs_code, default=self.ws.settings.default_crs))
        self.summary.setText("\n".join(lines))
        self.import_btn.setEnabled(self.ws.user.can("import_cadastre"))

    def _zoning_summary(self, records, m: FieldMapping) -> list[str]:  # type: ignore[no-untyped-def]
        from ...persistence.zoning_repo import _code

        zid, zname = m.columns.get("zone_id", ""), m.columns.get("zone_name", "")
        codes: dict[str, str] = {}
        blank = 0
        for r in records:
            c = _code(r.attrs.get(zid)) if zid else ""
            if c in ("", "0"):
                blank += 1
            else:
                codes.setdefault(c, " ".join(str(r.attrs.get(zname, "") or "").split()) if zname else "")
        lines = [tr("zon.summary", records=len(records), zones=len(codes), blank=blank,
                    invalid=sum(1 for r in records if not r.valid))]
        # Cross-check with the newest rule version, if any: unknown codes and differing names.
        versions = self.ws.rules.list_versions()
        if versions:
            rules = {r.code: r for r in self.ws.rules.rules(versions[0].id)}
            missing = sorted((c for c in codes if c not in rules), key=lambda c: (len(c), c))
            differ = [c for c, n in codes.items() if c in rules and n and n != " ".join(rules[c].name_ar.split())]
            lines.append(tr("zon.crosscheck", label=versions[0].label, missing=", ".join(missing) or "—",
                            differ=", ".join(differ) or "—"))
        return lines

    def _import(self) -> None:
        try:
            if self.kind == "zoning":
                res = self.run_busy(tr("cad.importing"), lambda: self.ws.zoning.import_layer(
                    self.ws.user, self.path, self._mapping(), self.name.text()))
                label = f"{res['name']} v{res['version']} ({res['records']})"
            else:
                layer = self.run_busy(tr("cad.importing"), lambda: self.ws.cadastre.import_layer(
                    self.ws.user, self.path, self._mapping(), self.name.text()))
                label = layer.label
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        QMessageBox.information(self, self.windowTitle(), tr("cad.imported", label=label))
        self.accept()


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
