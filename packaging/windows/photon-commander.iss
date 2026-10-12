; Per-user Windows installer. Mutable data is deliberately outside {app}.
#ifndef ReleaseVersion
  #error ReleaseVersion is required
#endif
#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
[Setup]
AppId={{C32C0E15-239D-44CC-A0AA-456827A9F8F9}
AppName=Photon Commander
AppVersion={#ReleaseVersion}
DefaultDirName={localappdata}\Programs\Photon Commander
DefaultGroupName=Photon Commander
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=Photon-Commander-{#ReleaseVersion}-win64
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\app\ssc-core.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=no
[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Photon Commander"; Filename: "{app}\app\ssc-core.exe"; Parameters: "--desktop"; WorkingDir: "{app}"
Name: "{group}\Installation and first-run guide"; Filename: "{app}\INSTALLATION.txt"
Name: "{group}\Uninstall Photon Commander"; Filename: "{uninstallexe}"
[Run]
Filename: "{app}\app\ssc-core.exe"; Parameters: "--desktop"; Description: "Open Photon Commander"; Flags: nowait postinstall skipifsilent
