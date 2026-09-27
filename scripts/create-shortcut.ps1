# Run this helper only when a desktop shortcut is wanted.
param([string]$PythonExecutable = '')
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$desktopPath = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopPath 'School Notifier.lnk'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    if (-not $PythonExecutable) {
        $PythonExecutable = (Get-Command python.exe -ErrorAction Stop).Source
    }
    $pythonPath = Join-Path (Split-Path -Parent $PythonExecutable) 'pythonw.exe'
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python was not found. Run scripts/setup.bat first.'
}
$ws = New-Object -ComObject WScript.Shell
$shortcut = $ws.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonPath
$shortcut.Arguments = '"' + (Join-Path $PSScriptRoot 'launch_desktop.py') + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.IconLocation = (Join-Path $projectRoot 'frontend\static\img\icon.ico') + ',0'
$shortcut.Description = 'School Notification Watcher'
$shortcut.WindowStyle = 7
$shortcut.Save()
Write-Output "Shortcut created: $shortcutPath"
