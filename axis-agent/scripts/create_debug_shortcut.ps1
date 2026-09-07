# Creates (or updates) an "AXIS (Debug).lnk" shortcut on the current
# user's Desktop that runs scripts/launch_axis_three_terminals.ps1 -
# the three-separate-visible-windows launcher, for when you want to watch
# the Temporal server, worker, and desktop app logs independently.
# Safe to re-run.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LauncherPath = Join-Path $PSScriptRoot "launch_axis_three_terminals.ps1"
$IconPath = Join-Path $RepoRoot "axis\desktop\assets\axis_icon.ico"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "AXIS (Debug).lnk"

if (-not (Test-Path $LauncherPath)) { throw "Launcher not found: $LauncherPath" }
if (-not (Test-Path $IconPath)) { throw "Icon not found: $IconPath" }

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = (Get-Command powershell.exe).Source
$shortcut.Arguments = "-NoLogo -ExecutionPolicy Bypass -File `"$LauncherPath`""
$shortcut.WorkingDirectory = $RepoRoot
$shortcut.IconLocation = $IconPath
$shortcut.Description = "AXIS (debug) - opens Temporal server, worker, and desktop app in separate visible terminals"
$shortcut.Save()

Write-Output "Created shortcut: $ShortcutPath"
