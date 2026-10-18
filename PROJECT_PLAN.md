# Survey & Licensing Studio — Project Plan

Local-first desktop application (Windows target, offline) for the land surveying and
building licensing department in Petra, Jordan. Combines a CAD workspace, a
geometry/setback calculation engine, a previous-license register, and multipage
Arabic/English PDF licensing packages.

Status legend: ✅ implemented and covered by automated tests · 🟡 partially implemented
(limits documented) · ⛔ not implemented / blocked (reason documented)

## Starting point (inspection, 2026-10-18)

- The GitHub repository `essamnawafleh-oss/survey-licensing-studio` was **empty**
  (no commits). There was no existing work to keep, so the project starts from scratch.
- Build environment: Linux container, Python 3.13. Target runtime is Python 3.12+
  on Windows. Already installed: openpyxl, ReportLab, pypdf, Pillow, pypdfium2.
  Added: PySide6, ezdxf, Shapely, arabic-reshaper, python-bidi, pytest, pytest-qt.
- No DWG SDK, no ODA File Converter and no OCR engine are installed. See the risks
  below and `TECHNICAL_DECISIONS.md`.

## Architecture (layers)

```
src/sls/
  core/          settings, i18n (ar/en), units & CRS model, error types   (no Qt)
  persistence/   SQLite + migrations, controlled file store, backups, audit log, repositories
  geometry/      pure calculation engine: validation, setbacks, areas, lengths   (no Qt)
  rules/         Excel rule-table inspection, column mapping, validation, snapshots
  licensing/     previous-license reconciliation, remaining-area calculation, eligibility checks
  cad/           drawing model (layers + entities), DXF import/export, DWG adapter, edit commands
  reporting/     Arabic shaping, vector drawing renderer, ReportLab report, attachment merge, manifest
  ui/            PySide6: main window, canvas, layer manager, inspector, command line, dialogs
```

Rules: `geometry`, `rules`, `licensing`, `cad` (model and IO) and `reporting` never
import Qt. The UI calls into them. Every calculation result carries its inputs, formula,
units, warnings and an input hash, so it can be reproduced later.

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| 1 | Foundation: window, structure, SQLite + migrations, cases, file store, dark UI, ar/en | see below |
| 2 | CAD workspace: DXF IO, layers, canvas, tools, measurements, properties | see below |
| 3 | Geometry engine: boundary validation, Excel rules, setbacks, areas, intersections, lengths | see below |
| 4 | Previous licenses: register, attachments, verification, optional local OCR | see below |
| 5 | Reporting: template, vector drawing, tables, Arabic PDF, photos, attachments, validation | see below |
| 6 | DWG integration and hardening, Windows packaging | see below |

The detailed per-feature status table is kept current in the "Feature status" section
at the end of this file.

## Key technical risks

1. **DWG compatibility.** ezdxf cannot read or write DWG. The real options are the ODA
   Drawings SDK (commercial, needs an ODA membership, C++ with an unofficial Python
   binding) or the free ODA File Converter executable (DWG↔DXF, driven through
   `ezdxf.addons.odafc`). Neither is present in the build environment, so DWG cannot be
   tested here. DWG stays ⛔ until acceptance tests pass on real DWG files.
2. **Legal setback geometry.** A plain negative buffer is wrong when different edges
   have different setbacks. The engine uses per-edge Euclidean distance zones and reports
   empty, split or sliver results explicitly. Edge classification (front/side/rear) is
   always confirmed by the user and never inferred silently.
3. **Units and CRS.** DXF `$INSUNITS` is often missing or wrong. Calculations refuse
   to run until the units have been verified by a person. Geographic (lat/long) CRSs are
   rejected for metric area.
4. **Arabic PDF.** ReportLab does not shape Arabic. We apply arabic-reshaper and
   python-bidi and embed Noto Naskh Arabic (OFL). Tests extract text and check glyph
   embedding.
5. **CAD rendering performance in Python.** Paths are cached in a local origin frame
   (survey coordinates are large) and painted through a single view transform.
6. **Data integrity.** Original files are never overwritten (content-hashed copies).
   Calculations are pinned to a rule version and to license record revisions.

## Feature status (2026-10-18, 108 automated tests passing, 2 DWG tests skipped)

| Area | Feature | Status | Evidence / notes |
|------|---------|--------|------------------|
| M1 | Desktop window, dark CAD theme, docks, toolbars, status bar | ✅ | `ui/`, `tests/test_ui_smoke.py`, screenshots inspected |
| M1 | SQLite schema, migrations, transactions, stable UUIDs | ✅ | `persistence/schema.py`, `tests/test_persistence.py` |
| M1 | Case creation/saving/reopening, configurable fields + editor | ✅ | `CaseRepository`, `CaseDialog`, `FieldsDialog` |
| M1 | Controlled file store (hash-named copies, never overwrite) | ✅ | `test_file_store_never_overwrites_and_verifies` |
| M1 | Arabic/English UI, RTL layout | ✅ | `tests/test_i18n.py` (key parity), AR screenshots |
| M1 | Automatic + manual backup, restore (UI + CLI) | ✅ | `test_backup_and_restore`, `test_cli_backup_list_and_restore` |
| M1 | Local accounts, roles, audit log | ✅ | Roles gate workflow actions only (TD-11) |
| M2 | DXF import with full entity accounting, layer/attribute preservation | ✅ | `tests/test_cad_dxf.py` |
| M2 | DXF export, round trip of IDs/meta/layers/units/bulges/dimensions | ✅ | `test_round_trip_preserves_ids_meta_layers_units` |
| M2 | Layer manager (vis/lock/colour/linetype/weight/transparency/role/floor/order/search) | ✅ | undoable via `LayerState` |
| M2 | Canvas: pan/zoom/extents, window/crossing select, grid, object/grid snap, ortho | ✅ | |
| M2 | Tools: line, polyline, rectangle, circle, move, copy, rotate, scale, offset, erase, dist, area, text, dimension, undo/redo | ✅ | `tests/test_cad_ops.py`, UI tests |
| M2 | Trim / extend | 🟡 | LINE entities only |
| M2 | Command line with aliases and coordinate input | ✅ | |
| M2 | Properties inspector (editable layer, coords, text, radius, floor, notes) | ✅ | |
| M3 | Boundary validation (closure, validity, degenerate, zero-area) | ✅ | `tests/test_geometry.py` |
| M3 | Excel rule import: sheet/header/mapping/validation/preview/immutable versions/verification | ✅ | `tests/test_rules_excel.py` |
| M3 | Per-edge setback geometry (non-convex, narrow, empty, slivers, missing rule stops) | ✅ | TD-06 |
| M3 | Floor areas (union, overlap/duplicate detection), setback intersections, outside-parcel | ✅ | |
| M3 | Wall/fence lengths (overlap counted once) | ✅ | |
| M3 | Calculation trace + reproducibility (input/result hashes) | ✅ | `test_calculation_is_reproducible_from_stored_inputs` |
| M3 | Preliminary eligibility checks (setbacks, coverage, floors, FAR) | ✅ | not a legal decision (TD-07) |
| M4 | License register, multiple documents, page refs, verification, revisions | ✅ | `LicensesDialog`, `test_license_edit_resets_verification_and_bumps_revision` |
| M4 | Reconciliation: verified-only, conflicts, duplicates, exceeding measured | ✅ | `tests/test_licensing_engine.py` |
| M4 | Optional offline OCR (Tesseract ara+eng), unverified notes only | ✅ | `tests/test_ocr.py` (ran with Tesseract 5) |
| M5 | Multipage PDF: cover, vector drawing at 1:N, tables, licenses, remaining, violations, notes, trace, photos, documents manifest | ✅ | `tests/test_reporting.py`, rendered pages inspected |
| M5 | Arabic shaping/bidi/mirroring, embedded Noto fonts | ✅ | |
| M5 | Page selection, attachments merge, missing/unsupported attachments reported | ✅ | |
| M5 | Draft/reviewed/approved with approval blockers; revisions kept; manifest JSON | ✅ | |
| M5 | Preview in app (QtPdf) | ✅ | |
| — | Regulatory plan: separate CRS, extent check, control-point transformation as new revision | ✅ | `tests/test_transform.py` |
| M6 | DWG via ODA File Converter | ⛔ unverified | adapter exists; acceptance tests skip without converter + real DWG |
| M6 | Windows packaging (PyInstaller spec + build script) | 🟡 | build-tested on Linux only |

### Next steps
1. Install ODA File Converter on a Windows test machine, add real (non-confidential) DWG
   files to `tests/fixtures/dwg/`, and run the DWG acceptance tests.
2. Build and smoke-test the Windows executable with `packaging/build_windows.ps1`.
3. With the department: confirm the setback corner convention, the official rule-table
   layout, the case form fields and the report title block. Then pilot with real (local,
   never committed) cases.
4. Trim/extend for polylines and arcs; translate engine diagnostic messages.
