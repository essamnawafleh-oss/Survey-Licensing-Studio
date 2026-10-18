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

## Feature status

Maintained as features land. See the bottom of README.md for user-facing limitations.
