# Build Setup.exe: PyInstaller build (with tests and self-test) followed by the Inno Setup compiler.
# Requires Inno Setup 6 (https://jrsoftware.org) — ISCC.exe on PATH or in its default folder.
$ErrorActionPreference = "Stop"
if (-not (Test-Path dist\SurveyLicensingStudio\SurveyLicensingStudio.exe)) {
    & .\packaging\build_windows.ps1
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$version = (Select-String -Path src\sls\__init__.py -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { Write-Error "Inno Setup 6 (ISCC.exe) not found"; exit 1 }
& $iscc "/DAppVersion=$version" packaging\installer.iss
if ($LASTEXITCODE -ne 0) { Write-Error "Inno Setup failed (exit $LASTEXITCODE)"; exit $LASTEXITCODE }
Write-Host "Built: dist\SurveyLicensingStudio-Setup-$version.exe"
