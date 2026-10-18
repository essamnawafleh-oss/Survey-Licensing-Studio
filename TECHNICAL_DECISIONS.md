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
