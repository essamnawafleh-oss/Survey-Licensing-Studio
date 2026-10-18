; Inno Setup script: Windows installer (Setup.exe) for Survey & Licensing Studio.
; Build:  ISCC.exe /DAppVersion=0.1.0 packaging\installer.iss   (after the PyInstaller build)
;
; - Installs the one-folder build from dist\SurveyLicensingStudio.
; - Installed in Program Files for all users after one administrator prompt, like most desktop
;   programs; Arabic or English wizard (Arabic.isl, chosen from the Windows display language).
; - User data (%LOCALAPPDATA%\SurveyLicensingStudio\data), settings (%APPDATA%) and logs are
;   never written, replaced or removed by the installer or the uninstaller.
; - Same AppId for every version: installing a newer version upgrades in place.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Survey & Licensing Studio"
#define AppExe "SurveyLicensingStudio.exe"
#define ConsoleExe "SurveyLicensingStudio-console.exe"
#define AppNameAr "استوديو المساحة والترخيص"

[Setup]
AppId={{8E3B5C2A-6F41-4C1D-9B7E-2D5A1F0C9E47}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Survey & Licensing Studio
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\Survey Licensing Studio
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Like most desktop applications (e.g. Blender): one administrator prompt, installed in Program Files
; for all users. (/CURRENTUSER on the command line still allows a per-user install, used by CI.)
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=SurveyLicensingStudio-Setup-{#AppVersion}
SetupIconFile=sls.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
InfoBeforeFile=installer_info.txt
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

ShowLanguageDialog=auto
LanguageDetectionMethod=uilanguage

[Languages]
; Arabic first: Windows with an Arabic display language gets the Arabic wizard automatically.
Name: "arabic"; MessagesFile: "compiler:Default.isl,Arabic.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; On upgrade, remove the previous version's bundled libraries so no stale DLL remains.
; Only the program's own folder is affected; user data never lives there.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\SurveyLicensingStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "installer_info.txt"; DestDir: "{app}"; DestName: "README-INSTALL.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "{#AppNameAr}"
Name: "{autoprograms}\{#AppName} self-test"; Filename: "{cmd}"; Parameters: "/k ""{app}\{#ConsoleExe}"" self-test"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
