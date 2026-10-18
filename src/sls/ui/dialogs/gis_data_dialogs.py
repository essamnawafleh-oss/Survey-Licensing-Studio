"""File → GIS data management: staged import/refresh, comparison, activation and rollback.

Parcels, roads and land-use zoning go through ``DatasetManager`` (one pipeline); land-use
rules go through the existing rule-table workflow (new immutable version + reviewer verification).
Nothing here activates a dataset without an explicit confirmation.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QTabWidget, QTextBrowser, QVBoxLayout,
                               QWidget)

from ...core.crs import PRESETS
from ...core.i18n import tr
from ...gis.shapefile_layer import FieldMapping, iter_records
from ...persistence.datasets import ADAPTERS, DatasetVersion, ImportControl
from ...persistence.workspace import Workspace
from .common import fill_table, make_table, show_error

KINDS = ("cadastre", "roads", "zoning")


def _default_run_task(run_busy):  # type: ignore[no-untyped-def]
    return lambda label, fn: run_busy(label, lambda: fn(ImportControl()))


class DatasetImportDialog(QDialog):
    """Choose a Shapefile, map fields, check, then validate & stage a new version (never activates)."""

    def __init__(self, ws: Workspace, run_busy, parent: QWidget | None = None,  # type: ignore[no-untyped-def]
                 kind: str = "cadastre", run_task=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.ws, self.run_busy, self.kind = ws, run_busy, kind
        self.run_task = run_task or _default_run_task(run_busy)
        self.adapter = ADAPTERS[kind]
        self.path: Path | None = None
        self.info = None
        self.staged: DatasetVersion | None = None
        self.activated: DatasetVersion | None = None
        self.setWindowTitle(tr(f"gis.import_title.{kind}"))
        self.resize(940, 680)
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
        crs_row = QHBoxLayout()
        self.crs = QComboBox()
        self.crs.setEditable(True)
        self.crs.addItem(tr("gis.crs_from_prj"), "")
        for code, label in PRESETS:
            self.crs.addItem(f"{code} — {label}", code)
        crs_row.addWidget(QLabel(tr("gis.declared_crs")))
        crs_row.addWidget(self.crs, 1)
        lay.addLayout(crs_row)
        self.meta = QLabel()
        self.meta.setWordWrap(True)
        lay.addWidget(self.meta)
        self.preview = make_table([])
        self.preview.setMaximumHeight(180)
        lay.addWidget(self.preview)
        g = QGroupBox(tr("cad.mapping"))
        f = QFormLayout(g)
        self.boxes: dict[str, QComboBox] = {}
        for fld in self.adapter.logical_fields:
            cb = QComboBox()
            self.boxes[fld] = cb
            f.addRow(tr(f"gis.field.{kind}.{fld}"), cb)
        lay.addWidget(g)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        row2 = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("cad.name_hint"))
        check = QPushButton(tr("cad.check"))
        check.clicked.connect(self._check)
        self.import_btn = QPushButton(tr("gis.stage"))
        self.import_btn.setEnabled(False)
        self.import_btn.clicked.connect(self._import)
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.reject)
        row2.addWidget(QLabel(tr("cad.name")))
        row2.addWidget(self.name, 1)
        for b in (check, self.import_btn, close):
            row2.addWidget(b)
        lay.addLayout(row2)
        active = ws.datasets.active(kind)
        if active is not None:
            self.meta.setText(tr("gis.active", label=active.label, at=active.imported_at, by=active.imported_by,
                                 act_at=active.activated_at, act_by=active.activated_by))

    def declared_crs(self) -> str:
        data = self.crs.currentData()
        if data is not None and self.crs.currentText() == self.crs.itemText(self.crs.currentIndex()):
            return data
        return self.crs.currentText().split(" ")[0].strip()  # typed by the user

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("cad.layer_file"), "", "Shapefile (*.shp)")
        if not path:
            return
        from ...gis.shapefile_layer import inspect

        try:
            self.info = inspect(Path(path))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.path = Path(path)
        self.file.setText(path)
        self.name.setText(self.name.text() or self.path.stem)
        i = self.info
        crs = i.crs_code or (tr("gis.crs_unrecognised") if i.crs_wkt else tr("gis.crs_missing"))
        missing = (" — " + tr("cad.missing", files=", ".join(i.missing_components))) if i.missing_components else ""
        self.meta.setText(tr("cad.meta", type=i.shape_type, n=i.count, crs=crs, enc=i.encoding) + missing)
        import shapefile

        with shapefile.Reader(str(self.path), encoding=i.encoding) as r:
            rows = [list(rec) for rec in r.records()[:20]]
        self.preview.setColumnCount(len(i.fields))
        self.preview.setHorizontalHeaderLabels(i.fields)
        fill_table(self.preview, rows)
        suggested = self.adapter.suggest(i.fields).columns
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
            self.adapter.check_mapping(m)
            info, validation = self.ws.datasets.inspect_source(self.kind, self.path, self.declared_crs())
            records = self.run_busy(tr("cad.reading"), lambda: list(iter_records(self.path, info.encoding,
                                                                                 self.adapter.shape_types)))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        s = self.adapter.summarize(records, m)
        lines = [summary_text(self.kind, s)]
        lines += [f"  • #{r.fid}: {r.issue}" for r in [r for r in records if not r.valid][:10]]
        if self.kind == "zoning":
            lines += self._zoning_crosscheck(s.get("codes", {}))
        lines.append(tr("gis.crs_line", crs=validation["crs"], source=validation["crs_source"]))
        lines += [f"! {w}" for w in validation["warnings"]]
        self.summary.setText("\n".join(lines))
        self.import_btn.setEnabled(self.ws.user.can(self.adapter.permission))

    def _zoning_crosscheck(self, codes: dict[str, str]) -> list[str]:
        versions = self.ws.rules.list_versions()
        if not versions:
            return []
        rules = {r.code: r for r in self.ws.rules.rules(versions[0].id)}
        missing = sorted((c for c in codes if c not in rules), key=lambda c: (len(c), c))
        differ = [c for c, n in codes.items() if c in rules and n and n != " ".join(rules[c].name_ar.split())]
        return [tr("zon.crosscheck", label=versions[0].label, missing=", ".join(missing) or "—",
                   differ=", ".join(differ) or "—")]

    def _import(self) -> None:
        try:
            self.staged = self.run_task(tr("gis.staging"), lambda control: self.ws.datasets.stage(
                self.kind, self.ws.user, self.path, self._mapping(), self.name.text(), self.declared_crs(), control))
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.activated = self.review(self.staged)
        self.accept()

    def review(self, staged: DatasetVersion) -> DatasetVersion | None:
        """Show the comparison; returns the version if the user activated it (overridable in tests)."""
        dlg = ComparisonDialog(self.ws, self.kind, staged.id, self)
        dlg.exec()
        return dlg.activated


# Kept for the File menu entries and existing callers: the same staged dialog.
CadastreImportDialog = DatasetImportDialog


def summary_text(kind: str, s: dict[str, Any]) -> str:
    if kind == "cadastre":
        return tr("cad.summary", **{k: s.get(k) if s.get(k) is not None else "—" for k in (
            "records", "invalid", "multipart", "missing_key", "repeated_keys", "repeated_key_records",
            "missing_parcel")})
    if kind == "zoning":
        return tr("zon.summary", records=s["records"], zones=s["zones"], blank=s["without_zone"], invalid=s["invalid"])
    return tr("gis.roads_summary", records=s["records"], invalid=s["invalid"], multipart=s["multipart"],
              types=", ".join(f"{k} {v}" for k, v in s["geometry_types"].items()),
              missing_id="—" if s["missing_id"] is None else s["missing_id"], repeated=s["repeated_ids"],
              missing_name=s["missing_name"], length=f"{s['total_length_m']:,.1f}")


def comparison_html(v: DatasetVersion, comp: dict[str, Any]) -> str:
    e = html.escape
    rows = [f"<h3>{e(v.label)} — {e(tr('gis.status.' + v.status))}</h3>",
            f"<p>{e(tr('gis.imported', at=v.imported_at, by=v.imported_by))}<br>CRS: <b>{e(v.crs_code)}</b> "
            f"({e(v.validation.get('crs_source', ''))})</p>"]
    for w in v.validation.get("warnings", []):
        rows.append(f"<p style='color:#c77700'>! {e(w)}</p>")
    rows.append(f"<p>{e(summary_text(v.kind, v.summary))}</p>")
    nulls = {k: n for k, n in (v.summary.get("null_fields") or {}).items() if n}
    if nulls:
        rows.append(f"<p>{e(tr('gis.null_fields'))}: " + ", ".join(f"{e(k)} {n}" for k, n in nulls.items()) + "</p>")
    against = comp.get("against")
    if against is None:
        rows.append(f"<p><b>{e(tr('gis.no_active'))}</b></p>")
        return "".join(rows)
    rows.append(f"<h4>{e(tr('gis.compared_with', label=against['label']))}</h4><ul>")
    rows.append(f"<li>{e(tr('gis.records', old=comp['records']['old'], new=comp['records']['new']))}</li>")
    if comp.get("identical_sources"):
        rows.append(f"<li><b>{e(tr('gis.identical'))}</b></li>")
    crs = comp.get("crs", {})
    if crs.get("changed"):
        rows.append(f"<li style='color:#c62828'><b>{e(tr('gis.crs_changed', old=crs['old'], new=crs['new']))}</b></li>")
    if comp.get("mapping_changed"):
        rows.append(f"<li style='color:#c77700'>{e(tr('gis.mapping_changed'))}: "
                    f"{e(json.dumps(comp['mapping'], ensure_ascii=False))}</li>")
    rows.append(f"<li>{e(tr('gis.basis', basis=comp.get('basis', '')))}: "
                + ", ".join(f"{e(tr('gis.diff.' + k))} {comp[k]}" for k in
                            ("added", "removed", "geometry_changed", "attributes_changed", "unchanged") if k in comp)
                + "</li>")
    for k in ("zone_codes_added", "zone_codes_removed"):
        if comp.get(k):
            rows.append(f"<li>{e(tr('gis.diff.' + k))}: {e(', '.join(comp[k]))}</li>")
    for k, d in (comp.get("summary") or {}).items():
        rows.append(f"<li>{e(k)}: {d['old']} → {d['new']}</li>")
    ex = comp.get("examples") or {}
    for k, ids in ex.items():
        if ids:
            rows.append(f"<li>{e(tr('gis.diff.' + k))} ({e(tr('gis.examples'))}): {e(', '.join(map(str, ids)))}</li>")
    if comp.get("identifier_changes"):
        rows.append(f"<li style='color:#c77700'>{e(tr('gis.id_changes', n=len(comp['identifier_changes'])))}: "
                    + ", ".join(f"{e(c['old_key'])}→{e(c['new_key'])}" for c in comp["identifier_changes"][:20])
                    + "</li>")
    sus = comp.get("suspicious_geometry_changes") or []
    if sus:
        t = comp["suspicious_thresholds"]
        rows.append(f"<li style='color:#c62828'>{e(tr('gis.suspicious', n=len(sus), pct=t['area_change_pct'], m=t['centroid_shift_m']))}<br>"
                    + "<br>".join(f"{e(str(s['id']))}: {s['area_old_m2']:.2f} → {s['area_new_m2']:.2f} m² "
                                  f"({s['area_change_pct']:.1f}%), {s['centroid_shift_m']:.2f} m" for s in sus[:20])
                    + "</li>")
    rows.append("</ul>")
    return "".join(rows)


class ComparisonDialog(QDialog):
    """Validation summary and comparison with the active version; explicit activation."""

    def __init__(self, ws: Workspace, kind: str, layer_id: str, parent: QWidget | None = None,
                 rollback: bool = False) -> None:
        super().__init__(parent)
        self.ws, self.kind, self.layer_id = ws, kind, layer_id
        self.activated: DatasetVersion | None = None
        self.version = ws.datasets.version(kind, layer_id)
        self.setWindowTitle(tr("gis.compare_title"))
        self.resize(820, 620)
        lay = QVBoxLayout(self)
        view = QTextBrowser()
        self.comparison = ws.datasets.compare(kind, layer_id)
        view.setHtml(comparison_html(self.version, self.comparison))
        lay.addWidget(view, 1)
        self.note = QLineEdit()
        self.note.setPlaceholderText(tr("gis.note"))
        lay.addWidget(self.note)
        self.confirm = QCheckBox(tr("gis.confirm"))
        lay.addWidget(self.confirm)
        row = QHBoxLayout()
        self.activate_btn = QPushButton(tr("gis.rollback_to") if rollback else tr("gis.activate"))
        self.activate_btn.setEnabled(False)
        self.activate_btn.clicked.connect(self._activate)
        self.confirm.toggled.connect(lambda on: self.activate_btn.setEnabled(
            on and ws.user.can(ADAPTERS[kind].permission) and self.version.status in ("staged", "inactive")))
        keep = QPushButton(tr("gis.keep_staged") if self.version.status == "staged" else tr("common.close"))
        keep.clicked.connect(self.reject)
        row.addStretch(1)
        if self.version.status == "staged":
            discard = QPushButton(tr("gis.discard"))
            discard.clicked.connect(self._discard)
            row.addWidget(discard)
        row.addWidget(self.activate_btn)
        row.addWidget(keep)
        lay.addLayout(row)

    def _activate(self) -> None:
        try:
            self.activated = self.ws.datasets.activate(self.kind, self.ws.user, self.layer_id, self.note.text())
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.accept()

    def _discard(self) -> None:
        if QMessageBox.question(self, tr("gis.discard"), tr("gis.discard_confirm")) != QMessageBox.Yes:
            return
        try:
            self.ws.datasets.discard(self.kind, self.ws.user, self.layer_id, self.note.text())
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.reject()


class GisDataDialog(QDialog):
    """Versions of every dataset, with import/refresh, comparison, activation and rollback."""

    def __init__(self, ws: Workspace, run_busy, parent: QWidget | None = None,  # type: ignore[no-untyped-def]
                 run_task=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.ws, self.run_busy = ws, run_busy
        self.run_task = run_task or _default_run_task(run_busy)
        self.setWindowTitle(tr("gis.title"))
        self.resize(1100, 640)
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tables: dict[str, Any] = {}
        self.active_labels: dict[str, QLabel] = {}
        self.rows: dict[str, list[DatasetVersion]] = {}
        for kind in KINDS:
            w = QWidget()
            v = QVBoxLayout(w)
            lab = QLabel()
            lab.setWordWrap(True)
            self.active_labels[kind] = lab
            v.addWidget(lab)
            t = make_table([tr("gis.col.version"), tr("gis.col.status"), tr("gis.col.records"), tr("gis.col.crs"),
                            tr("gis.col.imported"), tr("gis.col.activated"), tr("gis.col.sources")])
            self.tables[kind] = t
            v.addWidget(t, 1)
            row = QHBoxLayout()
            for key, slot in (("gis.import_refresh", self._import), ("gis.details", self._details),
                              ("gis.activate_selected", self._activate_selected), ("gis.rollback", self._rollback)):
                b = QPushButton(tr(key))
                b.clicked.connect(lambda _=False, s=slot, k=kind: s(k))
                row.addWidget(b)
            row.addStretch(1)
            v.addLayout(row)
            self.tabs.addTab(w, tr(f"gis.kind.{kind}"))
        self.tabs.addTab(self._rules_tab(), tr("gis.kind.rules"))
        lay.addWidget(self.tabs, 1)
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.accept)
        lay.addWidget(close)
        self.refresh()

    def _rules_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        note = QLabel(tr("gis.rules_note"))
        note.setWordWrap(True)
        v.addWidget(note)
        self.rules_table = make_table([tr("gis.col.version"), tr("gis.col.imported"), tr("gis.col.verified"),
                                       tr("gis.col.sources")])
        v.addWidget(self.rules_table, 1)
        row = QHBoxLayout()
        imp = QPushButton(tr("gis.rules_import"))
        imp.clicked.connect(self._rules_import)
        cmp_ = QPushButton(tr("gis.rules_compare"))
        cmp_.clicked.connect(self._rules_compare)
        row.addWidget(imp)
        row.addWidget(cmp_)
        row.addStretch(1)
        v.addLayout(row)
        return w

    def refresh(self) -> None:
        for kind in KINDS:
            versions = self.ws.datasets.versions(kind)
            self.rows[kind] = versions
            fill_table(self.tables[kind], [[x.label, tr(f"gis.status.{x.status}"), x.record_count, x.crs_code,
                                            f"{x.imported_at} {x.imported_by}", f"{x.activated_at} {x.activated_by}",
                                            ", ".join(f"{s['file']} {s['sha256'][:10]}" for s in x.sources)]
                                           for x in versions])
            a = self.ws.datasets.active(kind)
            self.active_labels[kind].setText(
                tr("gis.active", label=a.label, at=a.imported_at, by=a.imported_by, act_at=a.activated_at,
                   act_by=a.activated_by) if a else tr("gis.no_active"))
        self.rule_versions = self.ws.rules.list_versions()
        fill_table(self.rules_table, [[v.label, f"{v.imported_at} {v.imported_by}",
                                       f"{v.verified_at} {v.verified_by}" if v.verified else tr("gis.unverified"),
                                       f"{v.workbook_name} {v.workbook_sha256[:10]}"] for v in self.rule_versions])

    def _selected(self, kind: str) -> DatasetVersion | None:
        rows = sorted({i.row() for i in self.tables[kind].selectionModel().selectedRows()})
        return self.rows[kind][rows[0]] if rows else None

    def _import(self, kind: str) -> None:
        DatasetImportDialog(self.ws, self.run_busy, self, kind, self.run_task).exec()
        self.refresh()

    def _details(self, kind: str) -> None:
        v = self._selected(kind)
        if v is not None:
            ComparisonDialog(self.ws, kind, v.id, self).exec()
            self.refresh()

    def _activate_selected(self, kind: str) -> None:
        v = self._selected(kind)
        if v is None:
            return
        ComparisonDialog(self.ws, kind, v.id, self, rollback=v.status == "inactive").exec()
        self.refresh()

    def _rollback(self, kind: str) -> None:
        a = self.ws.datasets.active(kind)
        prev = None
        for v in self.rows.get(kind, []):
            if v.status == "inactive" and v.activated_at and (a is None or v.activated_at <= a.activated_at):
                if prev is None or v.activated_at > prev.activated_at:
                    prev = v
        if prev is None:
            QMessageBox.information(self, tr("gis.rollback"), tr("gis.no_previous"))
            return
        ComparisonDialog(self.ws, kind, prev.id, self, rollback=True).exec()
        self.refresh()

    def _rules_import(self) -> None:
        from .rules_dialog import RulesDialog

        RulesDialog(self.ws, self).exec()
        self.refresh()

    def _rules_compare(self) -> None:
        rows = sorted({i.row() for i in self.rules_table.selectionModel().selectedRows()})
        if not rows:
            return
        new = self.rule_versions[rows[0]]
        older = [v for v in self.rule_versions if v.rule_set_id == new.rule_set_id and v.version < new.version]
        if not older:
            QMessageBox.information(self, tr("gis.rules_compare"), tr("gis.no_previous"))
            return
        old = max(older, key=lambda v: v.version)
        c = self.ws.rules.compare_versions(old.id, new.id)
        text = tr("gis.rules_diff", old=old.label, new=new.label, added=", ".join(c["added"]) or "—",
                  removed=", ".join(c["removed"]) or "—", changed=len(c["changed"]), unchanged=c["unchanged"])
        details = "\n".join(f"{x['code']}: " + "; ".join(f"{k}: {d['old']} → {d['new']}" for k, d in x["changes"].items())
                            for x in c["changed"][:50])
        box = QMessageBox(self)
        box.setWindowTitle(tr("gis.rules_compare"))
        box.setText(text)
        box.setDetailedText(details or "—")
        box.exec()
