# GIS data management: import, refresh, activation and rollback

*File → GIS data management* manages every reference dataset the application uses:

| Dataset | Source | Pipeline |
|---|---|---|
| Parcels (cadastre) | Esri Shapefile, polygons | Shared dataset pipeline, parcel adapter |
| Roads / streets | Esri Shapefile, lines or polygons | Shared dataset pipeline, road adapter |
| Land-use zoning | Esri Shapefile, polygons with `ZONE_ID` | Shared dataset pipeline, zoning adapter |
| Land-use and setback rules | Excel workbook (`.xlsx`, `.xls`) | Existing rule-table workflow |

The older menu entries *File → Parcel register* and *File → Zoning register* open the same staged
import dialog.

## Supported input

- **Shapefile set.** These files must sit together in one folder:
  - `.shp`, `.shx` and `.dbf` are required. An import without them is refused before anything is
    written.
  - `.prj` is required for the coordinate system (see below).
  - `.cpg` is optional and gives the text encoding. Without it, cp1256 (Arabic Windows) is
    assumed and a warning is shown.
- **Geometry types:**
  - Parcels and zoning take polygons.
  - Roads take polylines or polygons.
  - Any other type (points, for example) is refused.
- **Attributes.** Every attribute of every record is stored verbatim with the feature. Mapped
  fields are also copied to indexed columns:
  - **Parcels:** village `VILL_CODE`, basin `HOD_CODE`, neighbourhood `SECTOR_COD`, sheet
    `SHEET_NO`, parcel `PARCEL_ID`, key `DLS_KEY`, `GLOBALID`.
  - **Zoning:** `ZONE_ID`, the land-use name, and optionally `DLS_KEY`.
  - **Roads:** road id, name, class, width.
- **Field mapping.** The dialog suggests a mapping from known field names. When a delivery
  renames a field (for example `DLS_KEY` becomes `PARCELKEY`), map it explicitly: unknown names
  are never guessed. Mapping a field that does not exist in the file is refused.

## Coordinate system requirements

- The coordinate system is read from the `.prj`. The authority's `Jordan_JTM` definition is
  identified as `ESRI:102158`, which is the same grid as `EPSG:3066`.
- **Missing or unrecognised `.prj`:** the import is refused until the coordinate system is
  declared explicitly in the dialog. It is never assumed.
- **A declaration that contradicts a valid `.prj`** is refused. Nothing is ever transformed or
  moved.
- **A layer in another grid** (for example UTM) can be staged, but:
  - the validation and the comparison flag it;
  - cases in the application grid will not use it, because parcel and road insertion check
    `same_grid`.
- The layer extents are checked against the coordinate system's area of use and reported.

## The refresh workflow

1. **Import / refresh…** in the dataset's tab. Choose the `.shp`, check the field mapping, then
   **Check**. Check lists:
   - records, invalid polygons, multipart features;
   - missing and duplicate identifiers;
   - empty values per mapped field;
   - CRS warnings;
   - for zoning, codes missing from the newest rule table.
2. **Validate and stage.** Every record is read and validated, and written as a new version with
   status *staged*.
   - The active version is not touched.
   - A failure or **Cancel** (a progress dialog with a cancel button is shown) rolls the whole
     version back and removes source copies made by that attempt. The failure is written to the
     audit log.
3. **Comparison.** The staged version is compared with the active one:
   - record counts and identical source files (re-import of an unchanged delivery);
   - CRS change and field-mapping change;
   - changes in the validation counts;
   - records added, removed, with geometry changed, or with attributes changed;
   - changed identifiers;
   - suspicious geometry changes.

   Records are compared by `GLOBALID` when it is mapped in both versions, otherwise by the DLS
   key, otherwise by geometry. Changed identifiers means the same `GLOBALID` now has a different
   DLS key. A geometry change is suspicious when the area changes by more than 5 % or the
   centroid moves more than 5 m.
4. **Activate.** Tick *I have reviewed the validation results and the comparison* and activate,
   adding a note for the audit log if wanted.
   - Activation is one database transaction: the previous version becomes *inactive* and the new
     one *active*. If anything fails, the previous version stays active.
   - SQLite query statistics are refreshed (`ANALYZE`). The search indexes cover every version,
     so searches use the new version immediately.
   - **Keep staged** leaves the version unused.
   - **Discard** removes a staged version's features but keeps its record: sources, hashes and
     summary.

Each version records:
- the dataset version number and name;
- import date and user;
- source file names with SHA-256 and size;
- feature count, field mapping and CRS, including whether the CRS came from the `.prj` or a
  declaration;
- the validation summary and the comparison;
- status, and activation date and user.

## Rollback

- **Roll back to previous…** re-activates the version that was active before the current one,
  using the same atomic activation and an audit entry (`rollback_dataset`).
- **Activate selected…** can re-activate any inactive or staged version, after showing its
  comparison with the active one.
- Versions are never edited or deleted, so a rollback loses nothing.

## History is never changed by a refresh

- Cases keep their own copies: the property boundary is copied into the drawing, the zoning
  overlay is copied into the calculation inputs, and the rule version is selected per case.
- Each calculation records the dataset versions it used, under `datasets`: the rule version, the
  parcel layer and parcel the boundary came from, the zoning layer, and any road layers inserted
  into the drawing.
- Nothing is recalculated automatically after a refresh. A case whose inputs now differ (for
  example a new zoning layer) shows as out of date until the user recalculates. The stored
  calculation, its report and its reproducibility check are unchanged.

## Roads

- Road layers are **reference data**. *Case → Insert GIS roads around the parcel* copies the
  features within 50 m of the boundary onto layer `GIS_ROADS`, with all attributes in the entity
  metadata.
- That layer has the *road* role. It may drive front-edge suggestions, but the user always
  confirms them.
- Roads are never used as a parcel boundary.

## Land-use and setback rules

- Each workbook import creates a new immutable rule version, using the usual mapping, preview,
  validation and reviewer verification.
- The *Rules* tab lists the versions and compares a version with the previous one: codes added
  or removed, and changed raw text or values.
- Conditional text is stored as text and never turned into a number.
- Cases keep the version they selected.

## Audit trail

The audit log records each of these actions with user, time, version and note: `stage_dataset`,
`stage_dataset_failed`, `activate_dataset`, `rollback_dataset` and `discard_dataset`.

## Not covered

- Real authority layers were imported and checked during development. They are confidential and
  are not in the repository; the tests use synthetic layers only.
- File geodatabases (`.gdb`), GeoPackage and other formats are not supported. Export to
  Shapefile first.
