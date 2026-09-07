# AXIS desktop launcher: makes the desktop shortcut behave like a real app
# by bringing up the two local prerequisites it needs (a Temporal dev
# server, the AXIS worker) only if they aren't already running, then
# starting the desktop UI. Safe to run repeatedly (e.g. by double-clicking
# the shortcut again) - it never starts a second Temporal server, worker,
# or a second desktop process talking to the same job.
#
# This is a convenience script only: it does not install anything as a
# Windows service, does not run in the background after AXIS closes on its
# own, and does not touch Temporal/worker credentials or configuration -
# it reads the same axis.yaml the app itself reads.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

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

# 1) Temporal dev server (localhost:7233) - only if nothing is listening.
if (-not (Test-Port "localhost" 7233)) {
    $TemporalExe = Resolve-TemporalExe
    if (-not $TemporalExe) {
        Write-Host "Could not find temporal.exe (checked PATH and the winget install location)."
        Write-Host "Install it with: winget install --id Temporal.TemporalCLI -e"
        exit 1
    }
    Write-Host "Starting a local Temporal dev server..."
    Start-Process -FilePath $TemporalExe -ArgumentList "server", "start-dev" -WindowStyle Minimized
    if (-not (Wait-ForPort "localhost" 7233 20)) {
        Write-Host "Temporal did not come up in time."
        exit 1
    }
} else {
    Write-Host "Temporal server already running on localhost:7233."
}

# 2) AXIS worker - only if no worker process for this repo is already up.
# A lightweight, safe-to-repeat check: is anything already polling the
# configured task queue? If the check itself fails (e.g. transient), fall
# through to starting a worker rather than silently doing nothing.
$workerNeeded = $true
try {
    $pollerCheck = & uv run python -c @'
import asyncio, sys
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
    Write-Host "Starting the AXIS worker..."
    Start-Process -FilePath "uv" -ArgumentList "run", "python", "-m", "axis.durability.worker" -WindowStyle Minimized
    Start-Sleep -Seconds 3
} else {
    Write-Host "An AXIS worker is already polling the task queue."
}

# 3) The desktop app itself, in the foreground - closing it does not stop
# the Temporal server or worker (matching "closing the app never cancels a
# running durable job").
uv run python -m axis.desktop
