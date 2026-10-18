"""Offline self-test for an installed or packaged copy: ``SurveyLicensingStudio-console.exe self-test``.

Exercises the functions a release must have, on *temporary* synthetic data only (the user's data
folder is only probed for write access, never modified): resource files, fonts and Arabic text,
PROJ coordinate systems, SQLite with migrations, backup and restore, DXF import/export, a full
synthetic calculation, the PDF report, GIS dataset staging/activation/rollback, Qt (PDF module),
and graceful handling of missing optional converters. Network access is blocked for the whole run
and any attempt is reported as a failure. Results are written as JSON next to the diagnostic log.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any


class _NetworkGuard:
    def __init__(self) -> None:
        self.attempts: list[str] = []

    def __enter__(self) -> "_NetworkGuard":
        self._connect, self._create = socket.socket.connect, socket.create_connection

        def blocked(*args: Any, **kwargs: Any) -> Any:
            self.attempts.append(repr(args[1:] if args else kwargs))
            raise OSError("network access is blocked during the self-test")

        socket.socket.connect = blocked  # type: ignore[method-assign]
        socket.create_connection = blocked  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        socket.socket.connect = self._connect  # type: ignore[method-assign]
        socket.create_connection = self._create  # type: ignore[assignment]


def run_self_test(report_path: Path | None = None) -> dict[str, Any]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from .core.diagnostics import LOGGER, log_dir, setup_logging, startup_report

    setup_logging()
    checks: list[dict[str, Any]] = []
    tmp = Path(tempfile.mkdtemp(prefix="sls-selftest-"))
    state: dict[str, Any] = {}

    def check(name: str, fn: Callable[[], str]) -> None:
        t0 = time.perf_counter()
        try:
            detail, ok = fn(), True
        except Exception as exc:  # noqa: BLE001 - every failure is recorded with its traceback
            detail, ok = f"{type(exc).__name__}: {exc}", False
            LOGGER.error("self-test %s failed\n%s", name, traceback.format_exc())
        checks.append({"name": name, "ok": ok, "detail": detail, "seconds": round(time.perf_counter() - t0, 2)})

    with _NetworkGuard() as guard:
        check("paths", lambda: _paths())
        check("resources_fonts_arabic", _resources)
        check("proj_jtm", _proj)
        check("sqlite_migrations_backup_restore", lambda: _sqlite(tmp, state))
        check("dxf_round_trip", lambda: _dxf(tmp))
        check("calculation_and_pdf_report", lambda: _calc_report(tmp, state))
        check("gis_stage_activate_rollback", lambda: _gis(tmp, state))
        check("qt_pdf_module", lambda: _qt(tmp, state))
        check("optional_converters", _converters)
    checks.append({"name": "no_network", "ok": not guard.attempts,
                   "detail": "no network access attempted" if not guard.attempts else
                   f"network attempts: {guard.attempts}", "seconds": 0})
    ws = state.get("ws")
    if ws is not None:
        ws.close()
    if all(c["ok"] for c in checks):
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)  # kept when something failed, for diagnosis
    result = {"ok": all(c["ok"] for c in checks), "started": datetime.now().isoformat(timespec="seconds"),
              "environment": startup_report(), "checks": checks, "temporary_folder": str(tmp)}
    report_path = report_path or log_dir() / f"selftest-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["report_path"] = str(report_path)
    except OSError as exc:
        result["report_path"] = f"not written: {exc}"
    return result


def _probe(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    probe = folder / f".sls-write-test-{os.getpid()}"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def _paths() -> str:
    from .core.settings import load_settings_checked, settings_path

    settings, problems = load_settings_checked()
    _probe(settings_path().parent)
    _probe(settings.data_path)
    install = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    data = settings.data_path.resolve()
    if install == data or install in data.parents:
        raise RuntimeError(f"the data folder {data} is inside the installation folder {install}")
    note = f"; settings problems: {problems}" if problems else ""
    return f"settings {settings_path()} and data {data} writable, outside the installation folder{note}"


def _resources() -> str:
    from .core.i18n import catalogue as _catalog
    from .reporting.arabic import has_arabic, register_fonts, visual

    register_fonts()
    ar, en = _catalog("ar"), _catalog("en")
    missing = sorted(set(en) - set(ar))[:5]
    if missing:
        raise RuntimeError(f"Arabic catalogue lacks keys: {missing}")
    v = visual("الارتداد الأمامي (front) = 4.00 م", "R")
    if not has_arabic(v) or "(front)" not in v:
        raise RuntimeError(f"Arabic shaping/ordering failed: {v!r}")
    return f"fonts registered; {len(en)} UI strings in both languages; Arabic shaping ok"


def _proj() -> str:
    from .core.crs import JTM_EPSG, JTM_ESRI, describe, same_grid, to_lonlat

    info = describe(JTM_ESRI)
    lon, lat = to_lonlat(JTM_ESRI, 353000.0, 355500.0)
    if not (34.5 < lon < 39.5 and 29 < lat < 33.5):
        raise RuntimeError(f"JTM point converted outside Jordan: {lon}, {lat}")
    if not same_grid(JTM_ESRI, JTM_EPSG):
        raise RuntimeError("ESRI:102158 and EPSG:3066 not recognised as the same grid")
    return f"{info.name}: 353000,355500 -> {lon:.5f}E {lat:.5f}N; EPSG:3066 same grid"


def _sqlite(tmp: Path, state: dict[str, Any]) -> str:
    from .core.settings import Settings
    from .persistence.backup import restore_backup
    from .persistence.schema import MIGRATIONS
    from .persistence.users import User
    from .persistence.workspace import Workspace

    admin = User("selftest-admin", "selftest", "Self test", "admin")
    ws = Workspace(Settings(data_dir=str(tmp / "data")), user=admin, auto_backup=False)
    state["ws"], state["admin"] = ws, admin
    version = ws.db.schema_version()
    if version != max(m[0] for m in MIGRATIONS):
        raise RuntimeError(f"schema version {version}")
    b = ws.backup_now("selftest")
    other = tmp / "restore" / "sls.sqlite3"
    other.parent.mkdir()
    other.write_bytes(b"")
    other.unlink()
    restore_backup(b, other)
    return f"schema v{version}; backup {b.name} verified and restored"


def _dxf(tmp: Path) -> str:
    from .cad.dxf_io import export_dxf, import_dxf
    from .samples import write_sample_dxf

    src = write_sample_dxf(tmp / "sample.dxf")
    d, report = import_dxf(src)
    export_dxf(d, tmp / "roundtrip.dxf")
    d2, _ = import_dxf(tmp / "roundtrip.dxf")
    if len(d2.entities) != len(d.entities):
        raise RuntimeError(f"{len(d.entities)} entities imported, {len(d2.entities)} after round trip")
    return f"{len(d.entities)} entities; export/re-import identical count; {len(report.actions)} report actions"


def _calc_report(tmp: Path, state: dict[str, Any]) -> str:
    from .core.units import CrsKind, DrawingUnits, LengthUnit
    from .persistence.cases import Case
    from .reporting.report import ReportOptions
    from .rules.excel_rules import ColumnMapping
    from .samples import (SAMPLE_EDGE_TYPES, SAMPLE_RULE_MAPPING, assign_sample_roles, write_sample_dxf,
                          write_sample_rules_xlsx)
    from .services.case_service import CaseService
    from .services.report_service import ReportService

    ws, admin = state["ws"], state["admin"]
    svc = CaseService(ws)
    v = ws.rules.import_version(admin, "Self-test", write_sample_rules_xlsx(tmp / "rules.xlsx"), "Setbacks", 2,
                                ColumnMapping.from_dict(SAMPLE_RULE_MAPPING))
    ws.rules.verify_version(admin, v.id)
    case = ws.cases.save(admin, Case(case_number="SELFTEST-1"))
    loaded = svc.import_drawing(case.id, write_sample_dxf(tmp / "case.dxf"))
    case = ws.cases.get(case.id)
    case.units = DrawingUnits(LengthUnit.METER, True, "synthetic-local", CrsKind.LOCAL, admin.username)
    case.rule_version_id, case.land_use_code = v.id, "SYN-B"
    ws.cases.save(admin, case)
    boundary = assign_sample_roles(loaded.drawing)
    ws.drawings.save_revision(admin, loaded.record.id, loaded.drawing, "classified")
    parcel = ws.drawings.parcel(case.id)
    parcel.boundary_entity_id = boundary
    parcel.boundary_points = [[p[0], p[1]] for p in loaded.drawing.entities[boundary].data["points"]]
    parcel.edge_types = SAMPLE_EDGE_TYPES
    ws.drawings.save_parcel(admin, parcel, verify=True)
    calc = svc.run_calculation(case.id)
    if calc.results["status"] != "ok":
        raise RuntimeError(f"calculation status {calc.results['status']}: {calc.results['errors']}")
    if not ws.calculations.reproduce(calc.id)[0]:
        raise RuntimeError("calculation did not reproduce")
    rs = ReportService(ws)
    res = rs.preview(case.id, ReportOptions(language="ar", generated_by="self-test"))
    if not res.pdf.startswith(b"%PDF"):
        raise RuntimeError("report is not a PDF")
    pdf = tmp / "report.pdf"
    pdf.write_bytes(res.pdf)
    state["pdf"] = pdf
    return (f"buildable {calc.results['setback']['buildable_area_m2']:.2f} m²; reproducible; Arabic PDF "
            f"{len(res.pdf) // 1024} KB")


def _gis(tmp: Path, state: dict[str, Any]) -> str:
    import shapefile

    from .gis.shapefile_layer import inspect, suggest_mapping

    ws, admin = state["ws"], state["admin"]
    prj = ('PROJCS["Jordan_JTM",GEOGCS["GCS_Jordan",DATUM["D_Jordan",SPHEROID["International_1924",6378388.0,'
           '297.0]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
           'PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",-3000000.0],PARAMETER["Central_Meridian",'
           '37.0],PARAMETER["Scale_Factor",0.9998],PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]')
    ids = []
    for n in (3, 4):
        folder = tmp / f"gis{n}"
        folder.mkdir()
        w = shapefile.Writer(str(folder / "P.shp"), shapeType=shapefile.POLYGON, encoding="utf-8")
        w.field("PARCEL_ID", "C", 5)
        w.field("DLS_KEY", "C", 15)
        for i in range(n):
            x = 353000 + 30 * i
            w.poly([[(x, 355500), (x, 355525), (x + 30, 355525), (x + 30, 355500), (x, 355500)]])
            w.record(f"{i + 1:05d}", f"SELFTEST{i:07d}")
        w.close()
        (folder / "P.prj").write_text(prj)
        (folder / "P.cpg").write_text("UTF-8")
        s = ws.datasets.stage("cadastre", admin, folder / "P.shp", suggest_mapping(inspect(folder / "P.shp").fields))
        ids.append(ws.datasets.activate("cadastre", admin, s.id).id)
    if len(ws.cadastre.find(parcel_no="4")) != 1:
        raise RuntimeError("refreshed parcel not found")
    back = ws.datasets.rollback("cadastre", admin, "self-test")
    if back.id != ids[0] or ws.cadastre.find(parcel_no="4"):
        raise RuntimeError("rollback did not restore the previous version")
    return "staged, compared, activated, refreshed and rolled back a synthetic parcel layer"


def _qt(tmp: Path, state: dict[str, Any]) -> str:
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    doc = QPdfDocument(app)
    pdf = state.get("pdf")
    if pdf is None:
        return "Qt started; PDF module loaded (no report to open)"
    doc.load(str(pdf))
    if doc.status() != QPdfDocument.Status.Ready or doc.pageCount() < 1:
        raise RuntimeError(f"QtPdf could not open the report: {doc.status()}")
    return f"Qt {app.platformName()} platform; QtPdf opened the report ({doc.pageCount()} pages)"


def _converters() -> str:
    from .cad.dwg import dwg_status
    from .core.settings import load_settings
    from .licensing.ocr import find_tesseract

    s = load_settings()
    tess = find_tesseract(s.tesseract_path)
    return f"DWG: {dwg_status(s.oda_converter_path, s.libredwg_path)} | OCR: " + \
        (f"Tesseract at {tess}" if tess else "Tesseract not installed (OCR disabled; manual entry still works)")


def main(report: Path | None = None) -> int:
    result = run_self_test(report)
    lines = [f"Survey & Licensing Studio self-test: {'PASSED' if result['ok'] else 'FAILED'}"]
    for c in result["checks"]:
        lines.append(f"  [{'ok' if c['ok'] else 'FAIL'}] {c['name']} ({c['seconds']} s): {c['detail']}")
    lines.append(f"Report: {result.get('report_path')}")
    text = "\n".join(lines)
    if sys.stdout is not None:
        print(text)
    return 0 if result["ok"] else 1
