# Creates (or updates) a "AXIS.lnk" shortcut on the current user's Desktop
# that runs scripts/launch_axis_desktop.ps1 with the AXIS icon. Safe to
# re-run - it overwrites the same shortcut file rather than creating
# duplicates.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LauncherPath = Join-Path $PSScriptRoot "launch_axis_desktop.ps1"
$IconPath = Join-Path $RepoRoot "axis\desktop\assets\axis_icon.ico"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "AXIS.lnk"

if (-not (Test-Path $LauncherPath)) { throw "Launcher not found: $LauncherPath" }
if (-not (Test-Path $IconPath)) { throw "Icon not found: $IconPath" }

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = (Get-Command powershell.exe).Source
$shortcut.Arguments = "-NoLogo -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$LauncherPath`""
$shortcut.WorkingDirectory = $RepoRoot
$shortcut.IconLocation = $IconPath
$shortcut.Description = "AXIS - durable browser-automation control surface"
$shortcut.Save()

Write-Output "Created shortcut: $ShortcutPath"
