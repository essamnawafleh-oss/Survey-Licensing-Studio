# Survey & Licensing Studio — استوديو المساحة والترخيص

A local-first desktop application for a land surveying and building licensing department.
It combines a CAD-style drafting workspace, an automated setback and area calculation
engine, a previous-license register, and multipage Arabic/English PDF licensing packages.
All processing is offline: no survey geometry or document ever leaves the workstation.

> **Status:** first release candidate (v0.1). DXF workflow, calculations, license register
> and PDF reporting are implemented and covered by automated tests. **DWG support is not
> verified** (see below). The Windows build script exists, but no Windows build has been made
> or tested yet. See `PROJECT_PLAN.md` for the per-feature status.

## Installation (Windows)

1. Install **Python 3.12 or newer (64-bit)** from python.org.
2. In PowerShell, from the repository folder:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   python -m sls
   ```
3. Optional:
   - **DWG:** install the free *ODA File Converter* (separate download from the Open Design
     Alliance, under ODA's own terms). Set its path under *File → Settings*.
   - **OCR:** install *Tesseract OCR* with the Arabic language data (for example the UB
     Mannheim Windows build). Set its path under *File → Settings*. OCR text is always stored
     as **unverified** and never fills any value.

**Packaging:** `packaging\build_windows.ps1` runs the tests and builds a one-folder
executable with PyInstaller (`dist\SurveyLicensingStudio\SurveyLicensingStudio.exe`). The
spec was build-tested on Linux and the frozen executable starts there. Building and testing
it on Windows is still to do.

## Data locations

| What | Default location |
|------|------------------|
| Settings | `%APPDATA%\SurveyLicensingStudio\settings.json` (override with the `SLS_CONFIG` environment variable) |
| Database + case files | `%LOCALAPPDATA%\SurveyLicensingStudio\data\` (configurable in Settings) |
| Case files | `data\cases\<case-id>\{drawing_source, drawing_revision, license_doc, photo, attachment, report}` |
| Rule workbooks | `data\shared\rule_workbook\` |
| Backups | `data\backups\` |

Imported files are **copied** into the store with a SHA-256 prefix. Originals are never
modified, and stored files are never overwritten.

## Workflow

1. **Create a case:** *File → New case*. Enter the case number, parcel, surveyor and any
   configured fields. Administrators can edit the form fields under *File → Case form fields*.
2. **Import the survey drawing:** *File → Import drawing* (DXF). The preview shows what
   happens to every entity type (imported, approximated, expanded or not imported), lists
   warnings, and suggests units from `$INSUNITS`. Assign each layer a **role** (building,
   wall, fence, road, property boundary, …) and a **floor** for building layers. A
   byte-for-byte copy of the source file is kept.
3. **Verify units and coordinate system:** *Case → Case details & units*. No area or
   setback is calculated until a user verifies them. Geographic (lat/long) coordinates are
   refused for metric area.
4. **Import the rule table:** *Case → Setback rule tables*. Choose the workbook (`.xlsx`, or
   legacy `.xls` such as the GIS export `ZONING_DESC_NEW`), the sheet and the header row.
   The column mapping is pre-filled from recognised headers (`ZONE_ID`, `NAME`,
   `SETBACK_FRONT/SIDE/BACK`, `BUILT_UP_AREA_PERCENT`, `FLOOR_NO`, `REMARK`); check it, then
   **Validate**.
   - Every value is kept exactly as written. A number is stored only when the text is
     unambiguous (`5`, `2.5`, `صفر`, `4م`, `39%`).
   - Each classification is marked *numeric*, *conditional* (e.g. `4م بعد 14م`,
     `صفر - 2`, `حسب المخطط`) or *no setbacks*.
   - Only structural errors block the import: missing or duplicate codes, negative values.
   - Each import is an immutable version that a reviewer must **verify**. Select the version
     and land-use classification in *Case details*.
5. **Conditional setback values:** for a conditional classification, the calculation stops
   and quotes the rule text. A reviewer opens *Case → Conditional setback values*, enters the
   value that applies to this parcel and writes a justification (required). Values from the
   rule table cannot be changed this way. The entry is audited, applies only to this case
   and rule version, and appears in the calculation trace and the report.
6. **Property boundary:** select the closed parcel polyline, then open *Case → Property
   boundary & edges*. Classify every edge (front, side, rear or a custom type from the rule
   table) and **Verify**. If the boundary changes later, the verification is invalidated.
7. **Previous licenses:** *Case → Previous licenses*. Enter licensed areas per floor,
   wall/fence lengths, approved violations and fees, and attach documents. A reviewer marks
   each record verified or disputed. Editing a verified record resets it to unverified.
8. **Calculate:** *Case → Calculate*. The results keep five categories apart: measured,
   previously licensed, remaining to assess, setback violation, and legally eligible (never
   decided by the software). The results also show eligibility checks, issues and a full
   trace (inputs, formula, unit, sources). *Add generated setback geometry* draws the
   buildable limit and violation areas on separate derived layers.
9. **Photos, attachments, notes:** *Case → Photos, attachments & notes*. Notes appear in the
   report and never change any calculated value.
10. **Report:** *Case → PDF report*. Set the options, **Preview**, then **Export revision**
   as draft, reviewed or approved. Approval is blocked until the calculation is current and
   complete, units and rules are verified, and no license is unverified. Every export is kept
   with its JSON manifest.

*File → Create demo case* builds a case from **synthetic** data and lists the verification
steps left for you to do.

### Regulatory plans

Import a regulatory plan with *Import as: Regulatory plan*. It is stored separately with its
own coordinates and is never moved automatically.
- *Case → Regulatory plan alignment check* compares the extents. Only if they overlap can
  the plan be inserted unchanged as `REG_*` reference layers.
- *Case → Regulatory plan transformation* fits a translation or similarity transformation
  from control-point pairs you enter, shows the per-point residuals and the RMS, and on
  confirmation saves the result as a **new revision** of the plan. The original revision is
  kept and the action is audited.

## CAD workspace

- **Navigation:** wheel to zoom at the cursor, middle-drag (or Alt+drag) to pan, *Zoom
  extents* (Ctrl+E / `ZE`). The coordinate system is Y-up.
- **Selection:** click; drag left→right for a window selection, right→left for a crossing
  selection; Shift to add or remove; Esc to clear.
- **Modes:** grid F7, object snap F3 (endpoint, midpoint, centre, quadrant, node), grid snap
  F9, ortho F8.
- **Coordinate input on the command line:** `x,y`, `@dx,dy`, `@dist<angle`; numbers for
  radius, angle, scale factor or offset distance; Enter to finish.

| Command | Aliases |
|---------|---------|
| Line / Polyline / Rectangle / Circle | `L` / `PL` / `REC` / `C` |
| Move / Copy / Rotate / Scale | `M` / `CO`,`CP` / `RO` / `SC` |
| Offset / Trim / Extend / Erase | `O` / `TR` / `EX` / `E`,`DEL` |
| Distance / Area / Text / Dimension | `DI` / `AA` (`O` = selected object) / `T`,`DT` / `DAL`,`DIM` |
| Undo / Redo / Zoom extents | `U` / `REDO` / `ZE` |
| Calculate / Report / Save | `CALC` / `REPORT` / `SAVE` |

The layer manager supports visibility, lock, colour, line type, line weight, transparency,
role, floor, ordering, create, rename, duplicate, delete, search and the current layer. Every
change is undoable.

## Backups and restore

- An automatic backup is taken once per day at startup and before any schema migration
  (retention is configurable). It uses SQLite's online backup API and an integrity check.
- *File → Back up database now*, or `python -m sls backup`.
- To restore: *File → Restore from backup* (admin), or `python -m sls restore <file>` with the
  application closed. The current database is always kept as
  `sls.sqlite3.before-restore-<timestamp>`. Case files are not touched.
- `python -m sls list-backups` lists the available backups.

## Security notes

- Optional local accounts (*Settings → Require login*, create the first admin with
  `python -m sls create-admin NAME`). Roles: surveyor, reviewer, admin. They control
  **workflow actions** such as verifying, approving and restoring.
- **This is not a security boundary.** Anyone with file access can read the SQLite database
  and the case files. Protect the workstation with Windows accounts, NTFS permissions and
  BitLocker.
- Changes to cases, units, rules, boundaries, licenses, calculations and reports are written
  to the `audit_log` table.

## DWG

ezdxf does not read or write DWG. DWG files are converted through the external ODA File
Converter, if installed, and then imported as DXF. The original DWG is kept. **This has not
been verified:** the acceptance tests in `tests/test_dwg_acceptance.py` need the converter
and real DWG files in `tests/fixtures/dwg/`, and skip until both exist. Full-fidelity native
DWG would need the commercial ODA Drawings SDK (see `TECHNICAL_DECISIONS.md`, TD-04).

## Development

```bash
pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m pytest -q     # Linux/CI; on Windows just: python -m pytest -q
```

The suite covers geometry, rules, licensing reconciliation, the calculation engine, DXF
round trips, persistence/backup/restore, PDF content (Arabic fonts, page numbers, values,
scale, attachments, reproducibility), OCR (skipped without Tesseract), i18n completeness,
architecture layering and offscreen UI workflows. All fixtures are **synthetic** (see
`src/sls/samples.py`). Never commit real survey or license data.

## Known limitations (v0.1)

- DWG is unverified (above). HATCH, 3D solids, images and paper-space entities are not
  imported; they are always listed in the import report, and the source file is kept.
- Trim and Extend operate on LINE entities only.
- Setbacks use the exact minimum-distance definition, which gives rounded inner corners.
  Other legal conventions, such as mitred corners, need confirmation from the authority
  before they are added.
- Diagnostic messages from the calculation engine are in English. Report and UI labels are
  in Arabic and English.
- Office documents cannot be embedded in the PDF offline; convert them to PDF first. They
  are reported as *not included*.
- UI language changes take effect after a restart.

## Licences of bundled assets

Noto Naskh Arabic and Noto Sans are bundled under the SIL Open Font License 1.1. The licence
texts are in `src/sls/resources/fonts/`.
