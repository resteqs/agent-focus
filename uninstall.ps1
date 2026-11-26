# Agent Focus uninstaller for Windows:
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
# Your config, notes, and synced collection stay in %LOCALAPPDATA%\agent-focus and %LOCALAPPDATA%\anki-headless.

$ErrorActionPreference = "Stop"

$RepoDir = $PSScriptRoot
$DataDir = if ($env:AGENT_FOCUS_DATA_DIR) { $env:AGENT_FOCUS_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "agent-focus" }
$TaskName = "AgentFocusDaemon"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed the $TaskName task."
}

$VenvPy = Join-Path $DataDir "venv\Scripts\python.exe"
$Py = if (Test-Path $VenvPy) { $VenvPy } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
& $Py (Join-Path $RepoDir "hooks\wire_hooks.py") uninstall

Remove-Item (Join-Path $DataDir "agent-focus.cmd") -ErrorAction SilentlyContinue
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath) {
    $Kept = @($UserPath -split ';' | Where-Object { $_ -and $_ -ne $DataDir })
    [Environment]::SetEnvironmentVariable("Path", ($Kept -join ';'), "User")
}

Write-Host "Agent Focus uninstalled. Personal config and scratchpad remain in $DataDir."
