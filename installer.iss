#define MyAppName "KQ Note"
#define MyAppVersion "1.4.4"
#define MyAppExeName "KQNote.exe"

[Setup]
AppId={{DBC88E9E-1B22-4433-884A-CCB385C06109}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=ckq7703
AppPublisherURL=https://github.com/ckq7703/KQ-Note
DefaultDirName={localappdata}\Programs\KQ Note
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer_output
OutputBaseFilename=KQNoteSetup
SetupIconFile=assets\logo-kqnote.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=*.exe
AppMutex=KQNoteAppMutex
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppName}
VersionInfoDescription={#MyAppName} Setup
VersionInfoProductName={#MyAppName}
VersionInfoCopyright=Copyright (c) 2026 ckq7703

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Tự động khởi chạy KQ Note cùng Windows"; GroupDescription: "Tùy chọn hệ thống:"

[Files]
Source: "dist\KQNote\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "KQNote"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Chạy {#MyAppName} ngay bây giờ"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
