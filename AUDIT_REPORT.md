# Code audit report — 2026-10-18 (branch `claude/foundation`)

## Scope and method

- **Areas audited:** geometry and calculations, CRS, CAD editing and DXF/DWG, persistence and
  backups, PDF reports, UI reliability, packaging.
- **Method:** three independent read-only audits, each with runnable reproduction scripts on
  synthetic data, followed by fixes.
- **Evidence standard:** a defect is listed as **fixed** only when one of these is true:
  - a regression test in `tests/test_audit_regressions.py` fails on the old code and passes on
    the new one (checked for every test listed);
  - the original reproduction script now gives the expected result (marked *script*).
- **Baseline before the audit:** 171 passed, 3 skipped (Linux, Python 3.13.16).
- **After the audit and the GIS work:** 227 passed, 3 skipped.
- **The 3 skips need external material:**
  - real DWG files and a converter (2 tests);
  - the authority's AutoCAD setback reference drawings (1 test).

Severity: **High** means data loss, wrong legal-geometric output, or a core workflow broken.
**Medium** means a wrong result or crash in a plausible situation. **Low** means minor or rare.

## Confirmed and fixed

### Geometry and calculation

| ID | Sev. | Defect | Reproduction | Fix | Test |
|---|---|---|---|---|---|
| A1 | High | Offset setback method raised a false "buildable area extends outside the parcel" error (not approvable) on ordinary convex parcels. Cause: GEOS overlay failure on coinciding edges. | Convex 4-gon with edges 0/4/0/4 m at JTM coordinates. | All construction overlays snap-rounded on a 1 nm grid; tolerant containment check. | `test_a1_…` |
| A2 | High | Offset method deleted the whole buildable area, depending on the boundary's start vertex. | Triangle with rear/rear/0 m, started at vertices 0, 1 or 2. | Strip cut computed per strip, not against the fragile union. | `test_a2_…` |
| A3 | High/Med | Setback zone invalid and larger than the parcel, so a footprint inside the buildable area was reported as a violation. | 6-gon from fuzzing. | Zone computed robustly and validated (buildable + zone = parcel) inside the construction. | `test_a3_…` |
| A9 | Low/Med | Floating-point noise slivers (about 1e-15 m²) produced "multipart" results and spurious review items in about 6 % of random parcels. | Fuzz cases. | Parts below 1 cm² are reported as noise (info issue), not counted as polygons. | `test_a9_…`; fuzz 0/1236 |
| A4 | Med | A building outside the parcel gave status ok and setbacks *pass*, with no issue. | Building on the neighbour's land. | Issue per floor; setbacks check fails and reports the outside area. | `test_a4_…` |
| A5 | Med | Adding generated geometry and saving made the calculation stale, which blocked report approval. | Derived layers, then save. | Staleness compares input content (`content_key`, without the revision id). | `test_a5_…` |
| A6 | Med | A footprint whose last vertex coincides with its first but is not flagged closed was dropped. | Open polyline with ends 0.2 mm apart. | Treated as closed, with a warning (1 mm tolerance). | `test_a6_…` |
| A7 | Med | Boundary dialog and engine counted edges differently (repeated closing vertex, near-duplicate vertex, arcs), so the calculation was blocked. | Three boundaries. | Shared `logical_boundary` normalisation; each arc's segments inherit its edge's classification. | `test_a7_…` |
| A8 | Med | A courtyard drawn as an inner outline was counted as built area without warning. | Nested outlines. | `nested_outline` warning. The area is unchanged; the user decides. | `test_a8_…` |
| A10 | Low | Circle on a wall or fence layer was silently ignored. | Circle on fence. | Counted. Other unsupported kinds on those layers are warned. | `test_a10_…` |
| A12 | Low | Mistyped projected CRS code skipped the extent check silently. | `EPSG:30666`. | `crs_unknown` warning. | `test_a12_…` |
| A14 | Med | Calculations saved before later input fields existed failed `reproduce()`. | Snapshot without `zoning`/`rule_override`. | The stored snapshot text itself is verified. | `test_persistence::test_snapshot_from_before…` |

These behaviour changes raise `ENGINE_VERSION` to 1.1.0. `reproduce()` now says when a stored
result was produced by an older engine and the current one differs. Stored calculations are never
changed (`test_engine_version_change_…`).

### CAD editing and DXF/DWG

| ID | Sev. | Defect | Fix | Test |
|---|---|---|---|---|
| C1 | High | An entity copied in AutoCAD after export (same stored id in XDATA) overwrote the original on import: silent entity loss. | Both kept; the copy gets a new id plus `duplicate_of`; reported. | `test_c1_…` |
| C2 | Med | Undo followed by a new edit left the drawing marked clean, so the edit could be lost on close. | Clean state invalidated. | `test_c2_…` |
| C3 | Med | A truncated DXF imported as an empty drawing without warning, or crashed with `StopIteration`. | Any parse failure falls back to recovery mode, which is reported; an empty recovery is refused. | `test_c3_…` |
| C4 | Med | ALIGNED/FIT/centred TEXT and MTEXT attachment were placed wrongly and the shift became permanent on export. | Alignment stored and written back. | `test_c4_c10_…` |
| C5 | Med | 2D POLYLINE with 0 or 1 vertices counted as imported, not damaged. | Reported as damaged with its handle. | `test_c5_…` |
| C6 | Med | Bounding boxes of bulge arcs over 180° were too small, so picking, selection and window clipping missed them. | Exact flattened bounds. | `test_c6_…` |
| C7 | Med | Entities on a case-variant layer name were invisible; renaming a layer orphaned entities. | Names normalised to the layer table; case-insensitive rename. | `test_c7_c9_…` |
| C8 | Med | Offset of a self-intersecting outline silently gave a wrong result. | Refused with explanation. | `test_c8_c13_…` |
| C9 | Med | DXF-illegal layer names were accepted, and then every DXF export failed. | Refused at entry. | `test_c7_c9_…` |
| C10 | Low/Med | Multi-line text lost its line breaks on export. | Exported as MTEXT. | `test_c4_c10_…` |
| C11 | Low/Med | Copy checked the active layer's lock instead of the source layer's, so copies landed on locked layers. | Each source layer checked. | *script* `t02` |
| C12 | Low/Med | `nan` / `inf` accepted as coordinates and values on the command line. | Refused. | `test_c12_…` |
| C13 | Low | Zero-length LINE crashed offset and extend. | Clear error. | `test_c8_c13_…` |
| C14 | Low | Non-executable or arbitrary file accepted as LibreDWG; raw `PermissionError`. | Validated; `OSError` reported. | `test_c14_c15_…` |
| C15 | Low | ODA "success" without an output file was not detected (export and import). | Checked. | `test_c14_c15_…`, *script* `t12b` |

Also fixed:
- An empty polyline is no longer silently dropped on export; it is listed.
- A damaged polyline is no longer reported as "unsupported type".

### Persistence, reports, settings, UI

| ID | Sev. | Defect | Fix | Test |
|---|---|---|---|---|
| P1 | High | Background work (drawing, parcel and zoning imports, run in a worker thread) failed in the GUI with SQLite thread errors. | One connection shared across threads, serialised by a re-entrant lock held for each transaction. | `test_p1_…` (3 tests, through the real `run_busy`/`run_task`) |
| P2 | High | Restore after a crash lost committed transactions still in the `-wal` file, also from the "preserved" copy. | Preserved copy made with the SQLite backup API. | `test_p2_…` (real crashed subprocess) |
| P3 | Med | Two restores in the same second overwrote the preserved copy. | Microsecond names; never overwritten. | `test_p3_…` |
| P4 | Med | Restore and open accepted newer-schema or non-application SQLite files. | Refused with explanation. | `test_p4_…` |
| P5 | Med | A failed first save (case, license, parcel) followed by a retry "succeeded" without writing anything. | Id reset when the transaction fails. | `test_p5_…` |
| P6 | Med | One corrupt photo aborted the whole report. | Decoded in the pre-check; listed as not included. | `test_p6_p7_p8_…` |
| P7 | Med | Photos were not checksum-verified (a replaced file was embedded silently). | Verified like documents. | `test_p6_p7_p8_…` |
| P8 | Low/Med | Multipage TIFF cut to page 1 silently; truncated JPEG attachment embedded. | Refused with reason. | `test_p6_p7_p8_…` |
| P9 | Med | Mixed Arabic/Latin text with brackets came out in the wrong order (`… front) = 4.00) …`). | Compiled UAX #9 ordering plus explicit bracket mirroring. | `test_p9_…` |
| P12 | Med | Cancel still attached the license document. | Respected. | `test_p12_…` |
| P13 | Low/Med | Report "save copy" crashed silently on a missing file; uncaught errors were invisible in the windowed exe. | Error shown; global exception hook logs and informs. | *script* `r10` |
| P14 | Med | Damaged `settings.json` (truncated, Notepad BOM, unknown language) stopped the app before any window. | Defaults used, file kept aside, user told. | `test_p14_p15_…` |
| P15 | Low | Wrong value types in settings crashed workspace opening. | Per-value fallback to defaults. | `test_p14_p15_…` |
| P17 | Low | A failed parcel or zoning import left copied source files in the store. | The new dataset pipeline removes copies made by the failed attempt. | `test_gis_datasets::test_failed_import_…` |
| P18 | Low | Each backup left `-shm`/`-wal` sidecar files. | Backups written as single files; immutable read-only verification. | `test_p18_p19_…` |
| P19 | Low | Retention deleted the pre-migration safety backup. | Only daily backups are pruned. | `test_p18_p19_…` |

## Remaining risks and open items (not fixed in this round)

**Calculation**
- **A11 (Low):** the recorded projection scale factor uses WGS84 lon/lat instead of the datum's
  own geodetic coordinates. The difference is about 1e-7. It is informational only; areas are
  grid areas.
- **A13 (Low):** license duplicate detection has four gaps:
  - values within the 0.005 tolerance count as conflicting;
  - license numbers are case-sensitive;
  - floor keys are not trimmed;
  - a conflict in one attribute blocks the floor area.
- The FAR and floor-count checks count every floor key, including basements and roofs. This is
  stated as an assumption in the check.

**Reports**
- **P10 (Low):** a single very long word (a hash or file name) can overflow a table cell.
- **P11 (Low):** symbols `∩ → ← ✓ ⚠` have no glyph in the bundled fonts.
- **P20 (Low):** drawing pages do not set "print scaling: none", so a printer's *fit to page* can
  break the 1:N scale. Print at 100 %.
- **Report approval workflow:**
  - the same user can approve without a reviewed revision;
  - older approved exports are not marked stale;
  - owner name, notes and photos are not part of the calculation hash.

**Persistence and audit**
- **P16 (Low/Med):** audit gaps:
  - user creation, login, backup, restore and OCR are not audited;
  - document, photo and attachment property changes are not audited;
  - `verify_version` on an unknown id writes an audit row.
- Restoring the database does not restore the file store.
- **Concurrent use:**
  - SQLite WAL on a network share is not supported;
  - two instances on one data folder are not prevented.

**CAD**
- Layer roles and floors are not written to DXF, so a DXF round trip resets them to *unassigned*.
- BYBLOCK colours are not inherited, and LWPOLYLINE width is dropped.
- Rotated text uses an unrotated box for window selection.
- Trim and Extend support LINE entities only (documented).
- Open-polyline offset uses `mitre_limit=10`, so very sharp corners are bevelled.

**DWG**
- Neither converter is installed in the test environment. DWG import remains **unverified**
  until it is compared against the source drawings in AutoCAD (`tests/test_dwg_acceptance.py`
  with `SLS_DWG_FIXTURES`).

**Setbacks**
- No comparison yet against the authority's approved AutoCAD workflow (harness ready:
  `tests/test_setback_reference.py`). No equivalence is claimed.

**Windows**
- Paths can exceed MAX_PATH without long-path support.
- Report preview and export run on the UI thread.
- Qt's own Arabic translations (standard buttons) are not loaded.

**Packaging and release:** see `docs/RELEASE_CHECKLIST.md`.
