#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef AppSource
  #error AppSource is required
#endif
#ifndef OutputPath
  #error OutputPath is required
#endif
[Setup]
AppId={{6E20F2D9-C889-4B92-9878-B204171598A2}
AppName=School Watcher
AppVersion={#AppVersion}
AppPublisher=jintairezhao
AppPublisherURL=https://github.com/jintairezhao/school-watcher
DefaultDirName={localappdata}\Programs\School Watcher
DefaultGroupName=School Watcher
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputPath}
OutputBaseFilename=School-Watcher-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\SchoolWatcher.exe
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE
[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#ifdef WebViewBootstrapper
Source: "{#WebViewBootstrapper}"; DestDir: "{tmp}"; Flags: deleteafterinstall
#endif
[Icons]
Name: "{group}\School Watcher"; Filename: "{app}\SchoolWatcher.exe"
Name: "{autodesktop}\School Watcher"; Filename: "{app}\SchoolWatcher.exe"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked
[Run]
#ifdef WebViewBootstrapper
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "正在准备网页显示组件…"; Flags: waituntilterminated runhidden; Check: NeedsWebView
#endif
Filename: "{app}\SchoolWatcher.exe"; Description: "打开 School Watcher"; Flags: nowait postinstall skipifsilent
[Code]
function NeedsWebView: Boolean;
var Version: String;
begin
  Result := not (RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKLM32, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version));
end;
// User data lives outside {app}; upgrades and uninstall never delete it.
