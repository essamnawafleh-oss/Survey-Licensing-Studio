# Windows release checklist — release candidate from `claude/foundation`

Status key:
- ✅ verified, with where it was verified
- ⏳ automated, but the result is still to be confirmed
- ⛔ not done yet (needs a person or a resource not available here)

**A successful PyInstaller build is not a release.** A release needs every row below to be ✅,
or explicitly accepted by the department.

## 1. Code and tests

| Check | Status | Evidence |
|---|---|---|
| Full test suite, Linux, Python 3.13.16 | ✅ | 229 passed, 3 skipped (DWG fixtures ×2, AutoCAD setback references ×1) |
| Full test suite, Windows (GitHub `windows-latest`, Python 3.12) | ✅ | [run 39157575558](https://github.com/essamnawafleh-oss/Survey-Licensing-Studio/actions/runs/39157575558). The previous run found 1 Windows-only failure (converter check, W1 in the audit report), now fixed. Skips: DWG ×2, AutoCAD references ×1, OCR ×2 (no Tesseract on the runner) |
| Dependency consistency (`pip check`) on Windows | ✅ | same run |
| Code audit and regression tests | ✅ | `AUDIT_REPORT.md`, `tests/test_audit_regressions.py` |
| GIS refresh data-integrity tests | ✅ | `tests/test_gis_datasets.py` |

## 2. Build

| Check | Status | Evidence |
|---|---|---|
| PyInstaller one-folder build from `packaging/sls.spec` (Linux) | ✅ | Build OK, 346 MB, both executables |
| PyInstaller build on Windows | ✅ | same run, step *PyInstaller build*; artifact *SurveyLicensingStudio-windows-rc* (95 MB zipped, kept until 2027-01-16) |
| Build script stops on any failing step | ✅ (by construction) | `packaging/build_windows.ps1` checks `$LASTEXITCODE` after each step; not run on a real machine |
| PROJ data bundled | ✅ Linux / ✅ Windows | Frozen self-test `proj_jtm`; log shows `proj_data` inside the bundle |
| Fonts and i18n bundled; Arabic shaping works when frozen | ✅ Linux / ✅ Windows | Self-test `resources_fonts_arabic` |
| Qt platform plugins and QtPdf bundled | ✅ Linux (offscreen) / ✅ Windows (offscreen) | Self-test `qt_pdf_module` opened the generated report |
| Optional converters missing are handled | ✅ | Self-test `optional_converters`; DWG/OCR absent gives a message, not a crash |

## 3. First launch and data safety

| Check | Status | Evidence |
|---|---|---|
| Frozen self-test in a clean environment (empty environment, fresh home) | ✅ Linux | `env -i HOME=<new>`: 10/10 checks passed |
| Frozen self-test as a new Windows user (empty `APPDATA`/`LOCALAPPDATA`) | ✅ | Same run: 10/10 checks; settings in `…\Roaming\SurveyLicensingStudio`, data in `…\Local\SurveyLicensingStudio\data` |
| User data outside the installation folder | ✅ Linux / ✅ Windows | Self-test `paths`; the workflow fails if `dist\…\data` appears |
| First launch of the windowed app creates the data folder, DB and backups folder, with no errors in the log | ✅ Linux (offscreen) | Fresh home: `sls.sqlite3`, `backups/`, `logs/`; 0 errors |
| SQLite initialisation, migrations, backup and restore | ✅ Linux / ✅ Windows | Self-test `sqlite_migrations_backup_restore`, plus tests |
| Updating does not overwrite user data | ✅ (design + tests) | Data and settings live outside the install folder. Newer schemas migrate after a pre-migration backup; a newer DB is refused (`test_p4_…`) |
| No network access in normal operation | ✅ | Self-test blocks sockets; 0 attempts during a full calculation, report, GIS import and backup |
| Diagnostic log and actionable errors | ✅ | `%LOCALAPPDATA%\SurveyLicensingStudio\logs\sls.log`; uncaught errors are logged and shown with the log path; a damaged `settings.json` gives a message, not a crash |

## 4. Functional smoke test on Windows (manual)

These still need a person at a Windows machine. None of them has been done yet.

| Check | Status | How |
|---|---|---|
| Install by copying `dist\SurveyLicensingStudio` to a clean Windows 10/11 account without Python | ⛔ | Unzip the CI artifact *SurveyLicensingStudio-windows-rc*, then run `SurveyLicensingStudio.exe` |
| Arabic UI renders correctly (RTL, fonts) on a real display | ⛔ | Open the app, check menus and dialogs |
| PDF preview and export on screen | ⛔ | Synthetic case → *Case → PDF report* → Preview → Export |
| DXF import and export with a real drawing | ⛔ | Import a department DXF; compare with CAD |
| GIS import/refresh/rollback with the real Petra layers | ⛔ | *File → GIS data management* with `JTM_VW` and `intersect` (local copies only) |
| Backup and restore from the UI | ⛔ | *File → Backup now*, then *Restore* |
| Settings created in `%APPDATA%`, data in `%LOCALAPPDATA%` | ⛔ (automated part ✅ in CI) | Check both folders after the first run |
| Upgrade test: run vN, create data, replace the folder with vN+1, data still present | ⛔ | Keep the data folder; replace only the install folder |
| Antivirus / SmartScreen behaviour of the unsigned executable | ⛔ | The executable is not code-signed |
| Long paths (deep case folders) | ⛔ | Without Windows long-path support, store paths can exceed 260 characters |

## 5. Not verified, and not claimed

- **DWG fidelity:** needs comparison against the source drawings in AutoCAD
  (`SLS_DWG_FIXTURES`).
- **Setback results against the authority's approved AutoCAD workflow:** needs reference
  drawings (`SLS_SETBACK_REFERENCES`).
- **OCR on Windows:** needs Tesseract to be installed. The 2 OCR tests are skipped on the CI
  runner.
- **Code signing, installer (MSI/Setup.exe), auto-update:** none. Distribution is a one-folder
  zip.
