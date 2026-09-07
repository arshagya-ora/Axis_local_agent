# Debug launcher: opens the three AXIS processes in three separate,
# visible terminal windows (Temporal dev server, AXIS worker, AXIS
# desktop app) so you can watch each one's live log output independently
# while testing. Idempotent for the server and worker - if either is
# already running, no duplicate window is opened for it. The desktop app
# always opens a fresh window (that's the one you're actively testing).
#
# Each window stays open after its process exits/crashes (-NoExit), so a
# traceback or error message doesn't disappear.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot

function Test-Port($HostName, $Port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $result = $client.BeginConnect($HostName, $Port, $null, $null)
        $success = $result.AsyncWaitHandle.WaitOne(500)
        if ($success -and $client.Connected) { $client.Close(); return $true }
        $client.Close()
        return $false
    } catch {
        return $false
    }
}

function Wait-ForPort($HostName, $Port, $TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $HostName $Port) { return $true }
        Start-Sleep -Milliseconds 300
    }
    return $false
}

function Start-NamedWindow($Title, $Command) {
    $fullCommand = "`$host.UI.RawUI.WindowTitle = '$Title'; Set-Location '$RepoRoot'; $Command"
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoExit", "-Command", $fullCommand -WindowStyle Normal
}

function Resolve-TemporalExe {
    # A shell opened before "winget install Temporal.TemporalCLI" never sees
    # the updated PATH (Windows only refreshes env vars for new top-level
    # sessions) - so PATH lookup alone is not reliable here. Fall back to
    # winget's own install location before giving up.
    $onPath = Get-Command temporal -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $wingetCandidates = Get-ChildItem -Path "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Temporal.TemporalCLI_*\temporal.exe" -ErrorAction SilentlyContinue
    if ($wingetCandidates) { return $wingetCandidates[0].FullName }
    return $null
}

$TemporalExe = Resolve-TemporalExe
if (-not $TemporalExe) {
    Write-Host "Could not find temporal.exe (checked PATH and the winget install location)."
    Write-Host "Install it with: winget install --id Temporal.TemporalCLI -e"
    exit 1
}

# 1) Temporal dev server.
if (-not (Test-Port "localhost" 7233)) {
    Write-Host "Opening a window for the Temporal dev server..."
    Start-NamedWindow "AXIS - Temporal server" "& '$TemporalExe' server start-dev"
    if (-not (Wait-ForPort "localhost" 7233 20)) {
        Write-Host "Temporal did not come up in time."
        exit 1
    }
} else {
    Write-Host "Temporal server already running on localhost:7233 - not opening a new window for it."
}

# 2) AXIS worker - only if nothing is already polling the task queue.
$workerNeeded = $true
try {
    $pollerCheck = & uv run python -c @'
import asyncio
from temporalio.client import Client
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.api.taskqueue.v1 import TaskQueue
from axis.config import load_axis_config

async def main():
    cfg = load_axis_config()
    c = await Client.connect(cfg.phase3.temporal.target, namespace=cfg.phase3.temporal.namespace)
    desc = await c.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(namespace=cfg.phase3.temporal.namespace,
                                  task_queue=TaskQueue(name=cfg.phase3.temporal.task_queue))
    )
    print(len(desc.pollers))

asyncio.run(main())
'@ 2>$null
    if ($LASTEXITCODE -eq 0 -and [int]$pollerCheck -gt 0) {
        $workerNeeded = $false
    }
} catch {
    $workerNeeded = $true
}

if ($workerNeeded) {
    Write-Host "Opening a window for the AXIS worker..."
    Start-NamedWindow "AXIS - Worker" "uv run python -m axis.durability.worker"
    Start-Sleep -Seconds 3
} else {
    Write-Host "An AXIS worker is already polling the task queue - not opening a new window for it."
}

# 3) The desktop app - always fresh.
Write-Host "Opening a window for the AXIS desktop app..."
Start-NamedWindow "AXIS - Desktop" "uv run python -m axis.desktop"
