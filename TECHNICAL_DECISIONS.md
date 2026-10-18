# Technical Decisions

Each decision records context, the choice, and its consequences. Add new entries at the end; do not rewrite history.

## TD-01 Stack: Python + PySide6 desktop, no server
- **Choice:** Python ≥3.12, PySide6 (LGPL) UI, SQLite, local files. No web server, cloud DB or external AI API.
- **Why:** Matches the brief; the department's data is confidential and the tool must run offline on Windows.
- **Consequence:** Packaging is done with PyInstaller (Milestone 6). PySide6 is LGPL: ship it as dynamically linked shared libraries, which PyInstaller does by default.

## TD-02 Layering and Qt isolation
- `geometry`, `rules`, `licensing`, `cad` (model/IO) and `reporting` are pure Python and never import Qt. This keeps the engine headless-testable and reusable from a CLI.

## TD-03 Own drawing model over ezdxf documents
- **Choice:** Imported DXF entities are converted into an internal model (`cad.model.Drawing`): layers plus a small set of entity types (line, polyline, circle, arc, text, point, dimension).
- **Why:** We need stable IDs, per-entity metadata (classification, floor, source vs derived), undo/redo and fast painting. An ezdxf document is not designed for that.
- **No silent loss:** Entities without an exact equivalent are either (a) approximated (ellipse, spline → polyline, flagged `approximated_from`), (b) expanded (INSERT/DIMENSION → their virtual entities, with the source recorded), or (c) not imported and **listed with counts** in the import report (e.g. HATCH, 3D solids, images). The original source file is always copied byte-for-byte into the case store, so nothing is ever lost.
- **Native format:** Each drawing revision is saved as JSON (`*.slsd.json`, lossless for the internal model) and can be exported to DXF R2018.

## TD-04 DWG
- ezdxf has **no** DWG reader or writer.
- Options evaluated:
  1. **ODA Drawings SDK:** full fidelity, but commercial (ODA membership, annual fee) with a C++ API, so it needs a binding. Not available here.
  2. **ODA File Converter:** a free executable that converts DWG↔DXF. ezdxf drives it through `ezdxf.addons.odafc`. It must be installed separately by the user, under ODA's own license terms.
- **Choice:** A pluggable `cad.dwg` adapter using ODA File Converter when its path is configured. The DWG goes through DXF and then the normal DXF import, and the original DWG is preserved.
- **Status:** ⛔ **Not verified.** The converter is not installed in the build environment, and no real DWG fixture is available. DWG acceptance tests (`tests/test_dwg_acceptance.py`) skip with an explicit reason until both exist. DWG must not be advertised as supported until those tests pass.

## TD-05 Units and coordinate reference systems
- Drawing units come from `$INSUNITS` only as a **suggestion**. A case stores `units` plus `units_verified` (who/when). The calculation engine raises `UnitsNotVerifiedError` if units are unknown or unverified.
- The CRS is a free-text identifier plus a `kind` field: projected, local or geographic. Geographic is rejected for metric area or length. No CRS is assumed from the project location.
- The survey and the regulatory plan are separate drawings, each with its own CRS. Alignment is an explicit, recorded similarity/affine transform, entered numerically or derived from user-picked control-point pairs with residuals shown. Nothing moves automatically.

## TD-06 Setback algorithm
- **Inputs:** a verified parcel polygon (closed, valid, non-self-intersecting), an explicit boundary type per edge (`front`, `side`, `rear`, or a custom key) confirmed by the user, and a verified rule version giving a distance for each boundary type.
- **Method:** the setback zone of edge *i* is `buffer(segment_i, d_i)`, the set of points closer than *d_i* to that edge. The buildable area is `parcel − ⋃ zones`. This is the exact "minimum Euclidean distance from each boundary line" definition. It handles non-convex parcels correctly, because no half-plane from one edge can cut off an unrelated part of the parcel. Different distances per edge work naturally.
- At reflex (inner) corners the result has a circular arc. That is the true minimum-distance locus. Authorities sometimes use mitred conventions instead. This is documented, and other conventions need confirmation from the authority before they are added.
- Explicit outcomes: `empty` (no buildable area), `multipart` (narrow sections split the area, reported with part areas), and sliver parts below a tolerance (reported, never dropped). Invalid inputs raise errors, and the engine never auto-repairs with `buffer(0)` behind the user's back.
- A missing rule value for any boundary type used by the parcel stops the calculation (`MissingRuleError`). There is no fallback.

## TD-07 Area categories are separate types
- Measured existing area, previously licensed area, remaining-to-assess area, setback-violation area and preliminary eligibility are separate fields with separate provenance. They are never added to or substituted for each other without an explicit formula step recorded in the calculation trace.
- Overlapping footprints on the same floor are unioned. The overlap amount is reported as a warning so it is visible and not double-counted.
- A setback intersection is a **geometric fact**. Licensing eligibility is a **separate preliminary rule check**, and the legal decision belongs to authorised staff.

## TD-08 Previous licenses
- Only `verified` license records feed the remaining-area calculation. Unverified records are listed as warnings.
- Licensed area greater than measured area for a floor is flagged as a conflict, and the result is **not clamped** to zero.
- A duplicate license number with differing values is flagged as a conflict and blocks that floor's remaining-area result.
- Summing several licenses on one floor assumes the licensed portions do not overlap. This assumption is stated in the trace.
- OCR, when available (local `tesseract` CLI only), stores extracted text as an **unverified note**. It never fills fields automatically.

## TD-09 Rule tables from Excel
- Workflow: inspect workbook → choose sheet and header row → map columns to logical fields (required: code, front, side, rear; optional: name_ar, name_en, max coverage %, max floors, max floor-area ratio, notes) → validate → preview → import.
- Validation errors (missing, non-numeric, negative, duplicate code) **block** the import. Rows are never skipped silently.
- Each import creates an immutable `rule_set_version` holding the workbook copy, its SHA-256, the sheet, the mapping and all rows. Calculations reference a version ID, so later imports never change historical results.
- A version must be verified by a user with the reviewer or admin role before a report can be marked approved.

## TD-10 Persistence
- SQLite in WAL mode with foreign keys on and ordered migrations tracked in `schema_migrations`. A backup is taken before migrating.
- Files live in `<data_dir>/cases/<case_uuid>/<category>/`. Copies are named `<sha256[:12]>_<original name>`. Existing files are never overwritten, and originals outside the store are never modified.
- Backups use the SQLite online backup API into `<data_dir>/backups/`: automatically once per day at startup and before migrations, with retention N. Restore is available from the UI and from the CLI (`python -m sls restore <file>`). The restore copies the current database aside first.
- Every write to license data, calculation inputs, rule verification or report status goes into `audit_log`.

## TD-11 Access control
- Optional local accounts with roles (`surveyor`, `reviewer`, `admin`), using PBKDF2-SHA256 password hashes. These roles guard **workflow actions** in the application, such as verification and approval.
- **This is not security.** Anyone with file-system access can read or modify the SQLite file. Real protection needs OS-level account separation and disk encryption (BitLocker). This is documented in the README.

## TD-12 Arabic PDF
- ReportLab plus arabic-reshaper and python-bidi, with Noto Naskh Arabic (SIL OFL 1.1, bundled with its license) embedded as TrueType subsets. Text direction is chosen per string, so mixed Arabic and Latin text with numbers is handled.
- Page numbers ("page X of Y") are stamped in a final pass after attachments are merged, so the totals are correct.
- Reports are reproducible: ReportLab runs in `invariant` mode, the generation timestamp is an explicit input, and tests compare bytes across runs.

## TD-13 Vector drawing in the PDF
- The drawing page is rendered directly from the internal model as ReportLab vector paths, at a user-chosen scale (1:N) or a computed "fit" scale rounded to a standard value. It includes a scale bar, a north arrow and a title block. It is never a screenshot.

## TD-14 Attachments in the PDF
- PDFs are merged with pypdf (all pages or a chosen page list). Images (JPEG, PNG, TIFF) are placed one per page. Office documents cannot be rendered offline without LibreOffice, so they are **reported as not included** on a "not included" page, in the manifest, and in the UI result. They are never dropped silently.

## TD-15 Canvas
- A custom `QWidget` with `QPainter` and a world→screen transform with Y up (surveying convention). Paths are cached per entity in a local origin frame to avoid float precision issues with large grid coordinates such as JTM eastings and northings. Hit testing and snapping run in world coordinates.

## TD-16 Bidi implementation
- python-bidi 0.6's compiled `bidi.get_display` does not mirror brackets (UBA rule L4), so "(م)" was rendered as ")م(". The pure-Python `bidi.algorithm.get_display` from the same package does mirror them and is used instead. This was found by rendering report pages and inspecting them.
- Noto Naskh Arabic has no Latin glyphs, so Noto Sans (OFL) is bundled too. Text is drawn as runs: Arabic-script characters in Naskh, everything else in Sans, with neutral characters following the previous run only when that font has the glyph.

## TD-17 Regulatory plan transformation
- A translation or a 4-parameter similarity (Helmert) transformation is least-squares fitted from control-point pairs that the user types in, solved in a centred frame for numerical stability. Per-point residuals and the RMS are shown before applying.
- Applying it creates a **new revision** of the regulatory drawing and records the parameters, the control points and who applied it in `drawing.info["transforms"]` and in the audit log. The survey drawing is never transformed, and visual or automatic alignment is never used.
- Affine and projective transforms, and datum transformations between national systems, are not implemented. They need confirmation of the authority's official parameters first.

## TD-18 Calculation inputs exclude derived geometry
- Generated setback and violation outlines (`meta.source = "derived"`) are excluded from the inputs, and from the input hash through `Drawing.source_content_hash()`. Adding them to the drawing therefore does not change the calculation result or mark the calculation stale.

## TD-19 Packaging
- PyInstaller one-folder build using `packaging/launcher.py`. The package's `__main__.py` cannot be the frozen entry script because of its relative imports, which the Linux test build revealed. QtWebEngine is excluded to keep the build small; QtPdf is kept for the report preview.

## TD-20 Real rule tables: raw text, conditional values, reviewer values (supersedes the strict part of TD-09)
- **Context:** the authority's land-use table (`ZONING_DESC_NEW`, exported from the GIS geodatabase as legacy `.xls`) has 133 classifications. Setbacks are stored as text, and about a quarter are conditional: "صفر - 2", "4م بعد 14م", "حسب المخطط", "صفر لطابقين و2م للبقية". Coverage values include "39%", "%15", "ضمن الارتدادات" and "30%بحد اقصى 100م2". Some classifications, such as archaeology zones and parks, define no values at all.
- Blocking the whole table, as TD-09 did, made the real table unusable. Guessing a number for conditional text would invent regulation.
- **Choice:**
  - Legacy `.xls` is read with `xlrd` 2.0.1 (read-only, offline).
  - Every value keeps its exact source text, and the whole source row is stored verbatim.
  - A number is stored only for unambiguous forms: plain numbers, Arabic-Indic digits, "صفر" (= 0), "4م", "39%" / "%15", or numeric cells with a % format. A number between 0 and 1 without a % format is treated as ambiguous.
  - Each row gets a status: `numeric`, `conditional` or `no_setbacks`. Only structural problems block the import: missing or duplicate codes, negative values, unmapped columns.
  - Any zero coverage or zero floor count is flagged for the reviewer, because it could mean "not permitted" or "not specified".
- **Calculation:** if an edge needs a boundary type whose rule text is conditional, the setback section stops with error `conditional_rule` and quotes the text. A reviewer (reviewer or admin role) can record the applicable values **for one case**, with a mandatory justification, in `case_setback_overrides`.
  - These reviewer values are tied to the exact rule version and code, so a new rule version does not inherit them.
  - They can never replace a numeric value from the rule table.
  - They are audited, included in the calculation inputs and hash, and shown in the trace, the issues list and the report (source text, applied value and basis per boundary type).
- Coverage, floor and FAR checks with non-plain text are `not_evaluated`, and the source text is given as the reason.
- The column mapping is pre-filled from recognised header names, such as `ZONE_ID`, `SETBACK_FRONT` and `BUILT_UP_AREA_PERCENT`, but this is only a suggestion that the user confirms.
- **Data handling:** the official table is not committed to the repository. Tests use synthetic tables with the same kinds of values.

## TD-21 Jordan TM and the coordinate system model (refines TD-05)
- **Context:** the authority's drawings, including the PDTRA regulatory plan (layers `PDTRA_JTM_*`), are in JTM. The plan's coordinates convert to Wadi Musa under EPSG:3066, which confirms the system.
- **Choice:** CRSs are resolved through pyproj and the PROJ/EPSG database (offline, shipped in the build). EPSG:3066 "ED50 / Jordan TM" is the default for new cases. Its parameters come from the registry, never from hand-typed values.
- A drawing's extents are checked against the CRS area of use at import, at units verification and in every calculation (issue `crs_extent`). This reliably catches wrong units, such as `$INSUNITS = inches` on GIS exports, and local coordinates. A failed check is only reported; no transformation is ever applied silently.
- Areas and lengths stay grid values in the verified planar system, which is the cadastral convention. The point scale factor (PROJ `get_factors`, rounded to 1e-7 so results are reproducible) is recorded in the trace for information only.
- The WGS 84 latitude/longitude in the status bar is display-only. It uses the datum transformation PROJ selects and is never used for any measurement.

## TD-22 DWG through LibreDWG as a second converter (updates TD-04)
- ODA's site is not reachable from the build environment. LibreDWG's `dwg2dxf` (from conda-forge here) is supported as a second converter, run as a separate program, so its GPLv3 licence does not extend to this application.
- **Findings on the real plan with LibreDWG 0.9.3:** 144 read errors were reported. 22 polylines came through with no vertices. 20,125 TEXT entities had corrupt alignment points of about (10, 20).
- The importer now does three things about this. It counts polylines with fewer than 2 vertices as *damaged*, with their handles. It ignores alignment points that lie implausibly far from the insertion point, counts them and records them on each entity. It copies every converter error line into the import report. Nothing is dropped silently.
- **DWG status:** import is verified on one real file with LibreDWG, including the known losses above. Writing DWG and ODA fidelity are still unverified.

## TD-23 Large drawings
- The plan has 43,500 entities and converts to a 128 MB DXF. ezdxf's fast loader is tried first, and its recover mode is used only on failure. The import runs in a worker thread behind a progress dialog.
- The canvas picks and snaps through a shapely STRtree spatial index, rebuilt lazily after edits. This cut pick plus snap from 4.7 s to about 0.1 s. Paint groups entities by layer, and the first paint caches all paths (about 4 s, once).
- Very dark layer colours are lightened for display only.
- Drawing revisions are serialised once. Integrity is checked on the stored bytes.
- A regulatory plan is inserted only around the survey: entities whose bounds touch the survey extents plus a margin are inserted whole and unchanged.

## TD-24 Parcel register from the GIS parcel layer
- **Context:** the department's parcel layer `JTM_VW` (Shapefile, 20,197 polygons) carries the DLS identifiers village, basin, sector, sheet, parcel and `DLS_KEY`, plus a `.prj` "Jordan_JTM". That is better than the DWG regulatory plan: attributes come per polygon, nothing is lost in conversion, and the coordinate system is explicit.
- **Choice:**
  - Read the layer with pyshp (pure Python, MIT) and shapely.
  - Store it in SQLite as an immutable, versioned `cadastre_layers` / `cadastre_parcels` register: WKB geometry, bounding-box columns and indexes. The source files are copied to `shared/cadastre/` with their SHA-256.
  - The text encoding comes from `.cpg`, falling back to cp1256 (Arabic Windows).
  - The CRS comes from `.prj` and is identified via PROJ (ESRI:102158). Layers are accepted for a case only when their grid matches the case CRS (`same_grid`: same projection, parameters, ellipsoid and unit). ESRI:102158 and EPSG:3066 therefore count as compatible.
- **Real data findings, all kept and shown, never repaired:**
  - 7 invalid polygons: 5 self-intersecting rings, and 2 with too few points or only holes.
  - 839 multi-part parcels.
  - 25 parcels without a key and 24 without a parcel number.
  - 60 keys repeated on 174 polygons. Example: `066403000000351` has 6 polygons of 750–1,215 m².
- **Rules:**
  - A search returns every matching polygon, and the user selects one.
  - A multi-part or invalid parcel gives no automatic boundary and comes with warnings.
  - Point picking takes the smallest polygon containing the point, and lists any others.
  - Using a parcel fills the case fields and sets an **unverified** boundary candidate. Register geometry carries `meta.source = "cadastre"`, the layer version and the GLOBALID.
- **Default CRS:** changed to ESRI:102158, the authority's own definition.

## TD-25 Zoning register (land-use zones per parcel)
- **Context:** the authority's `intersect` layer (Shapefile, 23,400 polygons, Jordan_JTM, UTF-8) is the parcel layer cut by the regulatory-plan zones. Each piece carries `ZONE_ID` (the land-use code of the rule table) and the land-use name. A parcel crossing a zone boundary appears as several pieces.
- **Choice:**
  - Import the layer as a separate, versioned register (`zoning_layers` and `zoning_pieces`, migration 4) with the same rules as TD-24: source files copied with SHA-256, nothing repaired, and permission `import_zoning` (reviewer and admin).
  - The zones of a parcel are computed **geometrically** (area of parcel ∩ zone polygons), not by matching keys. This works for repeated or missing keys and for drawn boundaries.
  - Overlaps below 1 m² or 0.5 % of the parcel are digitising **slivers**. They are recorded but never counted as a zone.
  - Pieces with `ZONE_ID` 0 or blank are reported as "zone polygon without code". Parcel area outside every piece is reported as a gap.
  - Invalid pieces are `buffer(0)`-ed only to measure the overlap. The stored geometry is unchanged.
  - The overlay is applied only when the case grid equals the layer grid (`same_grid`). A layer without `.prj` is assumed to be in the case grid.
- **Effect on the case:**
  - When a parcel is used from the register, the land-use code is filled in **only** if the parcel lies in exactly one coded zone, the case has no code yet, and the code exists in the case's rule version. Otherwise the user is told why it was not filled in, and nothing changes.
  - The calculation records the overlay in its inputs, so it is hashed and reproducible. It adds the trace step "zoning" and these warnings: `zoning_none`, `zoning_multiple` (one classification applied to a split parcel), `zoning_mismatch` (case code ≠ register) and `zoning_gap`.
  - The legal decision stays with the user: the register never overrides the selected classification.
- **Real data findings:**
  - 804 pieces have ZONE_ID 0.
  - Zone 71 does not exist in the rule table.
  - 1,592 parcel keys span several zones.
  - In a sample of 3,000 parcels: 2,709 lie in one zone, 147 in two, and 63 in none.
  - The import dialog cross-checks the codes and names against the newest rule version.
