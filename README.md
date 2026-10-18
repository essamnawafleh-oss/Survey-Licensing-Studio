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

**Packaging:** `packaging\build_windows.ps1` builds a one-folder executable with PyInstaller.
It stops at the first failing step: dependency check, test suite, build, then a self-test of the
frozen build. The result is two executables in `dist\SurveyLicensingStudio\`:
- `SurveyLicensingStudio.exe`, the windowed application;
- `SurveyLicensingStudio-console.exe`, for `backup`, `restore`, `create-admin` and `self-test`.

The GitHub Actions workflow `.github/workflows/windows-release-candidate.yml` runs the same build
on Windows and repeats the self-test under an empty user profile. See
[`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md) for what has been verified and what
still needs a manual check on a clean Windows machine.

**Self-test:** `SurveyLicensingStudio-console.exe self-test` (or `python -m sls self-test`)
checks a copy of the program offline, on temporary synthetic data:
- resources, fonts and Arabic text;
- PROJ (JTM);
- SQLite migrations, backup and restore;
- DXF round trip;
- a full calculation and the Arabic PDF report;
- GIS staging, activation and rollback;
- QtPdf;
- whether the optional DWG/OCR converters are present.

Network access is blocked during the run, and any attempt fails the test. The user's data is not
modified.

## Data locations

| What | Default location |
|------|------------------|
| Settings | `%APPDATA%\SurveyLicensingStudio\settings.json` (override with the `SLS_CONFIG` environment variable) |
| Database + case files | `%LOCALAPPDATA%\SurveyLicensingStudio\data\` (configurable in Settings) |
| Case files | `data\cases\<case-id>\{drawing_source, drawing_revision, license_doc, photo, attachment, report}` |
| Rule workbooks | `data\shared\rule_workbook\` |
| Backups | `data\backups\` |

| Diagnostic log | `%LOCALAPPDATA%\SurveyLicensingStudio\logs\sls.log` (rotating; override `SLS_LOG_DIR`). Records start-up details and every uncaught error, but no survey data |

User data and settings live outside the installation folder. Installing a new version over or
beside an old one therefore never touches them. A newer schema is migrated automatically, after
a pre-migration backup. A database from a *newer* version is refused, not opened.

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
   - **Setback method** (*Settings → Setback method*). The default is the surveyor's CAD
     workflow:
     - each boundary edge is offset inward by its own setback
     - consecutive offsets are trimmed/extended to their intersection, giving sharp corners,
       inner corners included
     - offsets of collapsed short edges are trimmed away, and closed narrow sections split
       the buildable area

     Where the setback changes along a nearly straight boundary, a perpendicular step is
     drawn where the larger setback begins. Very far-reaching corner intersections are
     listed for review.

     The previous method, minimum-distance zones (rounded inner corners), can still be
     selected. Each calculation records the method it used, and is always reproduced with it.
   - **Setback construction preview** (*Case → Setback construction preview…*). Shows each
     construction step as a separate layer that can be switched on and off:
     - the original boundary
     - each edge's offset line
     - the trimmed/extended lines, with trimmed portions in red and extended portions in yellow
     - the intersection points
     - discarded faces, each with its reason
     - the final buildable area

     Next to the drawing are the diagnostics and a per-edge audit table (classification,
     setback, rule version, trim/extend at each end, outcome).
     - Each diagnostic is *info*, *needs review* or *error*. Items that need review include:
       parallel offsets with different setbacks, remote intersections, collapsed or eliminated
       offsets, self-intersections, area cut by setback strips or clipped to the parcel,
       several polygons, and an empty result.
     - While any item needs review, the calculation gives **no buildable area**. A reviewer
       must accept the construction with a note. An *error* (validation failure) cannot be
       accepted and must be corrected.
     - *Cancel* changes nothing. *Accept* stores a reviewer-signed approval and writes the
       construction as derived layers (`SETBACK_OFFSETS`, `SETBACK_TRIM_EXTEND`,
       `SETBACK_TRIMMED`, `SETBACK_EXTENDED`, `SETBACK_CORNERS`, `SETBACK_DISCARDED`,
       `SETBACK_BUILDABLE`) in a new drawing revision. The surveyed boundary is never changed.
     - The approval lapses automatically if the boundary, an edge classification, a setback
       value, the rule version or classification, the units/CRS or the method changes. The
       calculation then says which one changed.
     - Results have not yet been compared with the authority's AutoCAD drawings, so no
       equivalence with AutoCAD is claimed (see *Development → setback references*).
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

## GIS data management (import / refresh / rollback)

*File → GIS data management* handles parcels, roads, land-use zoning and rule workbooks.
1. **Staged import.** An import first validates and stages a new version; the active one is
   untouched. The import is refused when sidecars are missing, and when a `.prj` is missing or
   unrecognised unless the coordinate system is declared. Field mapping, a progress dialog and
   Cancel are available.
2. **Comparison.** The staged version is compared with the active one: counts, added and removed
   records, changed geometry, attributes and identifiers, suspicious geometry changes, and CRS or
   mapping changes.
3. **Activation.** Activation is explicit and atomic, and audited. Rollback re-activates the
   previous version.
4. **History.** Calculations record the dataset versions they used, and nothing is recalculated
   automatically.

Full guide: [`docs/GIS_DATA_REFRESH.md`](docs/GIS_DATA_REFRESH.md).

## Parcel register (GIS parcel layer)

*File → Parcel register* imports the authority's parcel layer as an Esri Shapefile (`.shp`,
`.shx`, `.dbf`, `.prj` and, if present, `.cpg`, all in one folder).
- **Field mapping:** pre-filled for the DLS schema (`VILL_CODE`, `HOD_CODE`, `SECTOR_COD`,
  `SHEET_NO`, `PARCEL_ID`, `DLS_KEY`, `GLOBALID`); confirm it before importing.
- **Before importing**, a check lists every geometry problem, multi-part parcel, missing
  key or number, and key repeated on several polygons. Nothing is repaired, merged or
  dropped.
- **Versions:** each import is a new version, and the source files are kept with their
  SHA-256. The Petra district layer has 20,197 parcels and imports in about 8 seconds.

**Finding a parcel:** *Case → Find parcel* searches by village → basin → neighbourhood →
parcel number (`351` and `00351` both work), or by the DLS key. When several polygons share
the identifiers, all of them are listed with their areas, and the user selects one.
- *Use as property boundary* copies the polygon to `PROPERTY_BOUNDARY`, adds the
  neighbouring parcels with their numbers, and fills the case fields (village, basin,
  neighbourhood, parcel, sheet, DLS key). The boundary is a **candidate**: classify the edges
  and verify it as usual.
- The **Pick parcel** tool (`PP`) does the same with a click on the map.

### Zoning register (land-use zones)

*File → Zoning register* imports the authority's land-use layer (e.g. `intersect.shp`): one
polygon per zone piece with `ZONE_ID`, the land-use code of the rule table, and its name.
- **Before importing**, the dialog compares the codes with the newest rule table. It lists
  codes missing from the table (e.g. `71`) and names that differ.
- **Find parcel** shows the zones of each result with their share, for example
  `2 سكن فئة ب (100%)`. Parcels split by a zone boundary list every zone. Overlaps under
  1 m² or 0.5 % are ignored as digitising slivers.
- **Using a parcel** fills in the land-use classification automatically only when the parcel
  lies in a single zone whose code exists in the case's rule table. In every other case a
  message explains why, and nothing is changed.
- **The calculation** compares the case classification with the register and warns about a
  mismatch, a split parcel, no zone, or an uncovered area. The report includes a zone table.
  The register informs, and the applied classification is always the one chosen in the case.

## Coordinate system: Jordan TM (JTM)

New cases use the authority's GIS definition **"Jordan_JTM" (ESRI:102158, from the parcel
layer's `.prj`)** by default. **EPSG:3066 "ED50 / Jordan TM"** has the same projection and
gives identical E/N; only the datum used for latitude/longitude differs, by a few metres. That is Transverse Mercator, central meridian 37°E, scale factor 0.9998, false easting
500 000 m, false northing −3 000 000 m, International 1924 ellipsoid. Another system can be
chosen in *Case details*.
- **At import:** the drawing's coordinates are checked against Jordan, the area of use of
  JTM. GIS exports often declare *inches* in the DXF/DWG header. When the coordinates fit JTM
  in metres, metres are **suggested**, and you still verify them. Drawings in local
  coordinates or other units fail the check and are reported. Nothing is moved.
- **Status bar:** shows JTM E/N, and an approximate WGS 84 latitude/longitude for
  orientation only.
- **Calculations:** use JTM grid coordinates. The trace records the point scale factor at
  the site (≈ 1.00006 at Wadi Musa, so ellipsoidal areas differ by about 0.013%) for
  information. If the coordinates fall outside the CRS area of use, the calculation is
  flagged.
- **Pick parcel** (`PP`): after inserting the regulatory plan around the survey, click
  inside a parcel to copy its polygon to `PROPERTY_BOUNDARY`, together with the parcel
  number found inside it. Then classify the edges and verify, as usual.

## DWG

ezdxf does not read DWG. DWG files are converted to DXF by an external converter and then
go through the normal import with full entity accounting. The original DWG is kept.
1. **ODA File Converter** (free, from the Open Design Alliance) is preferred: it has the
   best fidelity and can also write DWG.
2. **LibreDWG `dwg2dxf`** (GNU GPLv3, run as a separate program) is the alternative.

Tested on the PDTRA regulatory plan (AutoCAD 2018 DWG, 21 MB, 43,500 entities) with LibreDWG
0.9.3. All layers, 23,379 parcel/road/wadi polylines and 20,127 parcel numbers came through
in JTM. The converter reported 144 read errors:
- 22 large polylines arrived without vertices and are listed as *damaged* in the import
  report.
- Every parcel-number text had a corrupt alignment point; this is detected and the
  insertion point is used.

A newer LibreDWG (0.13) or ODA should reduce these losses. Always compare damaged items with
the source in AutoCAD.

**The import report is actionable.** Besides the per-type counts (imported, approximated,
expanded, skipped), it lists what to check:
- comparison with the source DWG in AutoCAD, with the converter's read errors;
- damaged polylines and texts with corrupt alignment, by handle;
- unsupported types (HATCH fills, 3D faces, solids, meshes) that must be redrawn or exploded;
- approximated curves;
- paper-space content that is not imported;
- undeclared units;
- copies with duplicated ids.

The original file is never modified.

**DWG is not claimed to be faithful.** A conversion completing does not make it so. For real
acceptance, convert the authority's drawings and compare them against the source in AutoCAD:
1. layer list and entity counts per layer against the import report;
2. a few known coordinates;
3. every item the report lists as damaged.

Record the result before relying on DWG input. The acceptance tests (`tests/test_dwg_acceptance.py`) run against real
DWGs kept outside the repository (`SLS_DWG_FIXTURES`, `SLS_LIBREDWG`, `SLS_ODA_CONVERTER`).
Writing DWG still needs ODA.

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

### Setback reference drawings (AutoCAD comparison)

`tests/test_setback_reference.py` compares the construction with drawings made with the
authority's approved AutoCAD workflow. None is bundled: the drawings are confidential and
none has been supplied yet.
- Put pairs of `<name>.dxf` and `<name>.json` in a local folder outside the repository and
  set `SLS_SETBACK_REFERENCES` to that folder.
- The DXF holds the parcel on layer `PARCEL` (its vertex order is the edge order) and the
  approved buildable boundary on layer `BUILDABLE_APPROVED`.
- The JSON holds the edge classifications, the setbacks, `tolerance_m` and the source.

The test checks the Hausdorff distance between the boundaries and the difference in area.

## Known limitations (v0.1)

- DWG is unverified (above). HATCH, 3D solids, images and paper-space entities are not
  imported; they are always listed in the import report, and the source file is kept.
- Trim and Extend operate on LINE entities only.
- Setbacks: the surveyor offset method (sharp corners) is the default. The minimum-distance
  method (rounded inner corners) remains selectable. Which convention is legally binding
  should be confirmed with the authority. Parcels with holes are refused by both methods.
- Diagnostic messages from the calculation engine are in English. Report and UI labels are
  in Arabic and English.
- Office documents cannot be embedded in the PDF offline; convert them to PDF first. They
  are reported as *not included*.
- UI language changes take effect after a restart.

## Licences of bundled assets

Noto Naskh Arabic and Noto Sans are bundled under the SIL Open Font License 1.1. The licence
texts are in `src/sls/resources/fonts/`.
