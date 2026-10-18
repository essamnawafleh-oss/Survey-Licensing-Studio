# Build the Windows distribution folder (run from the repository root in PowerShell).
# Requires 64-bit Python 3.12 or 3.13 on Windows. Every step must succeed: native commands do not
# stop PowerShell on failure by themselves, so each exit code is checked.
$ErrorActionPreference = "Stop"
function Step($what, [scriptblock]$cmd) {
    Write-Host "== $what"
    & $cmd
    if ($LASTEXITCODE -ne 0) { Write-Error "$what failed (exit $LASTEXITCODE)"; exit $LASTEXITCODE }
}
Step "create build environment" { python -m venv .venv-build }
Step "upgrade pip" { .\.venv-build\Scripts\python -m pip install --upgrade pip }
Step "install dependencies" { .\.venv-build\Scripts\python -m pip install -r requirements.txt -r requirements-dev.txt "pyinstaller==6.*" }
Step "dependency check" { .\.venv-build\Scripts\python -m pip check }
Step "test suite (must pass before packaging)" { .\.venv-build\Scripts\python -m pytest -q }
Step "PyInstaller build" { .\.venv-build\Scripts\pyinstaller packaging\sls.spec --noconfirm }
# Self-test of the frozen build on temporary data (no network, user data untouched).
Step "frozen self-test" { .\dist\SurveyLicensingStudio\SurveyLicensingStudio-console.exe self-test --report dist\selftest.json }
Write-Host "Built: dist\SurveyLicensingStudio\SurveyLicensingStudio.exe (self-test report: dist\selftest.json)"
