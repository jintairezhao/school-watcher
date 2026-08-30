# Create desktop shortcut for School Notification Watcher
# ASCII-only paths (see HANDOFF.md pitfall 7.44: never use Chinese filenames across shells)
$ErrorActionPreference = 'Stop'

$desktop = 'C:\Users\Jinta\Desktop'
$lnkName = 'School Notifier.lnk'
$target  = 'd:\Jinta\Documents\Claude Code\school-watcher\scripts\school-notifier.bat'
$workdir = 'd:\Jinta\Documents\Claude Code\school-watcher'
$icon    = 'd:\Jinta\Documents\Claude Code\school-watcher\frontend\static\img\icon.ico'

$ws = New-Object -ComObject WScript.Shell
$shortcut = $ws.CreateShortcut((Join-Path $desktop $lnkName))
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $workdir
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'School Notification Watcher'
$shortcut.Save()

Write-Output "Shortcut created: $(Join-Path $desktop $lnkName)"
Write-Output "TargetPath:       $target"
Write-Output "WorkingDirectory: $workdir"
Write-Output "IconLocation:     $icon"
