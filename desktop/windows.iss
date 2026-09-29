#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef AppSource
  #error AppSource is required
#endif
#ifndef OutputPath
  #error OutputPath is required
#endif
#ifndef AppIdentifier
  #define AppIdentifier "{{6E20F2D9-C889-4B92-9878-B204171598A2}"
#endif
#ifndef LocationRegistry
  #define LocationRegistry "Software\SchoolWatcher"
#endif
[Setup]
AppId={#AppIdentifier}
AppName=School Watcher
AppVerName=学校通知 {#AppVersion}
AppVersion={#AppVersion}
AppPublisher=jintairezhao
AppPublisherURL=https://github.com/jintairezhao/school-watcher
DefaultDirName={localappdata}\Programs\School Watcher
DisableDirPage=no
UsePreviousAppDir=yes
UsePreviousTasks=yes
DisableStartupPrompt=yes
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
[Languages]
Name: "zhcn"; MessagesFile: "languages\ChineseSimplified.isl"
[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#ifdef WebViewBootstrapper
Source: "{#WebViewBootstrapper}"; DestDir: "{tmp}"; Flags: deleteafterinstall
#endif
[InstallDelete]
; Only obsolete application-owned browser files, never the user data directory.
Type: filesandordirs; Name: "{app}\_internal\browser-runtime"
Type: files; Name: "{app}\_internal\config\desktop-schools.json"
Type: files; Name: "{app}\_internal\config\schools.yaml"
[Icons]
Name: "{group}\School Watcher"; Filename: "{app}\SchoolWatcher.exe"
Name: "{autodesktop}\School Watcher"; Filename: "{app}\SchoolWatcher.exe"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
[Run]
#ifdef WebViewBootstrapper
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "正在准备网页显示组件…"; Flags: waituntilterminated runhidden; Check: NeedsWebView
#endif
Filename: "{app}\SchoolWatcher.exe"; Description: "打开学校通知"; Flags: nowait postinstall skipifsilent; Check: not IsUpgrade
Filename: "{app}\SchoolWatcher.exe"; Flags: nowait; Check: IsUpgrade
[Code]
var
  DataPage, FilesPage: TInputDirWizardPage;
  RemoveData: Boolean;
  Updating: Boolean;

function OpenProcess(Access: LongWord; Inherit: Boolean; ProcessId: LongWord): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function HasSwitch(Name: String): Boolean;
var I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/' + Name) = 0 then begin
      Result := True;
      Exit;
    end;
end;

function PreviousProgramDirectory: String;
var Key: String;
begin
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
    ExpandConstant('{#AppIdentifier}') + '_is1';
  if not RegQueryStringValue(HKCU64, Key, 'Inno Setup: App Path', Result) then
    if not RegQueryStringValue(HKCU32, Key, 'Inno Setup: App Path', Result) then Result := '';
  if not FileExists(AddBackslash(Result) + 'SchoolWatcher.exe') then Result := '';
end;

function IsUpgrade: Boolean;
begin
  Result := Updating;
end;

function SavedDirectory(Name, Fallback: String): String;
begin
  if not RegQueryStringValue(HKCU, '{#LocationRegistry}', Name, Result) or (Result = '') then
    Result := Fallback;
end;

procedure InitializeWizard;
var Previous: String;
begin
  Previous := PreviousProgramDirectory();
  Updating := (Previous <> '') and not HasSwitch('CHANGELOCATIONS') and
    (CompareText(AddBackslash(Previous), AddBackslash(WizardDirValue())) = 0);
  if Updating then begin
    WizardForm.Caption := '更新 - 学校通知 {#AppVersion}';
    Log('Updating the existing installation; keeping all personal file locations.');
  end;
  WizardForm.TasksList.Left := ScaleX(8);
  WizardForm.TasksList.Width := WizardForm.TasksList.Parent.ClientWidth - ScaleX(16);
  { Windows themes draw a DPI-sized glyph, while the checklist reserves a
    legacy bitmap width. Give the glyph space INSIDE the owner-drawn control. }
  WizardForm.TasksList.Offset := ScaleX(16);
  WizardForm.TasksList.MinItemHeight := ScaleY(28);
  WizardForm.RunList.Offset := ScaleX(16);
  WizardForm.RunList.MinItemHeight := ScaleY(28);
  DataPage := CreateInputDirPage(wpSelectDir, '数据位置', '选择订阅、收藏与设置的保存位置',
    '更改位置时会迁移现有数据，并保留原目录副本。', False, '');
  DataPage.Add('数据文件夹：');
  DataPage.Values[0] := SavedDirectory('DataDirectory', ExpandConstant('{localappdata}\SchoolWatcher'));
  FilesPage := CreateInputDirPage(DataPage.ID, '其他文件位置', '选择缓存、备份与更新包的保存位置',
    '可使用默认位置，也可以在安装后更改。', False, '');
  FilesPage.Add('缓存：');
  FilesPage.Add('自动备份：');
  FilesPage.Add('更新包：');
  FilesPage.Values[0] := SavedDirectory('CacheDirectory', DataPage.Values[0]);
  FilesPage.Values[1] := SavedDirectory('BackupDirectory', AddBackslash(DataPage.Values[0]) + 'backups');
  FilesPage.Values[2] := SavedDirectory('DownloadDirectory', AddBackslash(DataPage.Values[0]) + 'updates');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { Normal upgrades go straight to progress. Fresh installs and an explicit
    location change keep the complete wizard. }
  Result := Updating;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if Updating and (CurPageID = wpInstalling) then begin
    WizardForm.PageNameLabel.Caption := '正在更新';
    WizardForm.PageDescriptionLabel.Caption := '正在更新学校通知，完成后会自动重新打开。';
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var ProcessId: Integer; Handle: THandle; WaitResult: LongWord;
begin
  Result := '';
  if HasSwitch('UPDATE') and not Updating then begin
    Result := '未找到要更新的原安装目录。请重新打开学校通知后重试，或运行安装包重新选择位置。';
    Exit;
  end;
  { The old launcher exits after its services and profile lock are released.
    Wait for its executable handle before replacing files. }
  ProcessId := StrToIntDef(ExpandConstant('{param:WATCHERPID|0}'), 0);
  if ProcessId > 0 then begin
    Handle := OpenProcess($00100000, False, ProcessId);
    if Handle <> 0 then begin
      WizardForm.StatusLabel.Caption := '正在等待应用退出…';
      WaitResult := WaitForSingleObject(Handle, 60000);
      CloseHandle(Handle);
      if WaitResult <> 0 then
        Result := '学校通知尚未完全退出，本次未更新。请退出应用后重试。';
    end;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var OldData: String;
begin
  Result := True;
  if CurPageID = DataPage.ID then begin
    OldData := SavedDirectory('DataDirectory', ExpandConstant('{localappdata}\SchoolWatcher'));
    if FilesPage.Values[0] = OldData then FilesPage.Values[0] := DataPage.Values[0];
    if FilesPage.Values[1] = AddBackslash(OldData) + 'backups' then FilesPage.Values[1] := AddBackslash(DataPage.Values[0]) + 'backups';
    if FilesPage.Values[2] = AddBackslash(OldData) + 'updates' then FilesPage.Values[2] := AddBackslash(DataPage.Values[0]) + 'updates';
  end;
end;

function LocationArguments(Param: String): String;
begin
  Result := '--configure-locations --data-dir ' + AddQuotes(DataPage.Values[0]) +
    ' --cache-dir ' + AddQuotes(FilesPage.Values[0]) + ' --backup-dir ' + AddQuotes(FilesPage.Values[1]) +
    ' --download-dir ' + AddQuotes(FilesPage.Values[2]);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Code: Integer;
begin
  if (CurStep = ssPostInstall) and not Updating then
    if not Exec(ExpandConstant('{app}\SchoolWatcher.exe'), LocationArguments(''), '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      RaiseException('文件位置更改未完成，原数据仍保留。请退出学校通知，并选择空文件夹后重试。');
end;

function StopInstalledApplication: Boolean;
var Code: Integer;
begin
  Result := True;
  if not FileExists(ExpandConstant('{app}\SchoolWatcher.exe')) then Exit;
  Result := Exec(ExpandConstant('{app}\SchoolWatcher.exe'), '--stop-for-uninstall', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
  if not Result then
    SuppressibleMsgBox('后台进程未能退出，卸载已停止。请重启电脑后重试。', mbError, MB_OK, IDOK);
end;

function InitializeUninstall: Boolean;
var Form: TSetupForm; KeepButton, DeleteButton, CancelButton: TNewButton; LabelText: TNewStaticText; Choice: Integer;
begin
  RemoveData := False;
  Result := True;
  if UninstallSilent then begin
    Result := StopInstalledApplication();
    Exit;
  end;
#if VER >= EncodeVer(6, 6, 0)
  Form := CreateCustomForm(ScaleX(470), ScaleY(155), False, False);
#else
  Form := CreateCustomForm();
  Form.ClientWidth := ScaleX(470);
  Form.ClientHeight := ScaleY(155);
#endif
  try
    Form.Caption := '卸载学校通知';
    LabelText := TNewStaticText.Create(Form);
    LabelText.Parent := Form;
    LabelText.SetBounds(ScaleX(24), ScaleY(24), ScaleX(420), ScaleY(58));
    LabelText.AutoSize := False;
    LabelText.WordWrap := True;
    LabelText.Caption := '是否保留个人数据？' + #13#10 + '保留后，重新安装可继续使用订阅、收藏与设置。';
    KeepButton := TNewButton.Create(Form);
    KeepButton.Parent := Form;
    KeepButton.SetBounds(ScaleX(24), ScaleY(100), ScaleX(125), ScaleY(32));
    KeepButton.Caption := '仅卸载程序';
    KeepButton.ModalResult := mrYes;
    KeepButton.Default := True;
    DeleteButton := TNewButton.Create(Form);
    DeleteButton.Parent := Form;
    DeleteButton.SetBounds(ScaleX(159), ScaleY(100), ScaleX(185), ScaleY(32));
    DeleteButton.Caption := '删除程序及个人数据';
    DeleteButton.ModalResult := mrNo;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.SetBounds(ScaleX(354), ScaleY(100), ScaleX(92), ScaleY(32));
    CancelButton.Caption := '取消';
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    Form.Width := CancelButton.Left + CancelButton.Width + ScaleX(24) + Form.Width - Form.ClientWidth;
    Form.Height := CancelButton.Top + CancelButton.Height + ScaleY(24) + Form.Height - Form.ClientHeight;
    Choice := Form.ShowModal();
    Result := (Choice = mrYes) or (Choice = mrNo);
    RemoveData := Choice = mrNo;
    if RemoveData then
      Result := MsgBox('将删除当前应用的订阅、收藏、设置、缓存与自动备份。此操作无法撤销。继续？', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  finally
    Form.Free();
  end;
  if Result then Result := StopInstalledApplication();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Code: Integer;
begin
  if (CurUninstallStep = usUninstall) and RemoveData then begin
    if not Exec(ExpandConstant('{app}\SchoolWatcher.exe'), '--remove-personal-data', '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      RaiseException('个人数据未能全部删除。请退出学校通知后重试；卸载已停止。');
  end;
end;

function NeedsWebView: Boolean;
var Version: String;
begin
  Result := not (RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKLM32, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version));
end;
// Silent uninstall and the default choice retain all personal data.
