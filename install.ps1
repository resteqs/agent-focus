# Agent Focus installer for Windows. Run from the repo folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
# macOS and Linux use install.sh.

$ErrorActionPreference = "Stop"

$RepoDir = $PSScriptRoot
$DataDir = if ($env:AGENT_FOCUS_DATA_DIR) { $env:AGENT_FOCUS_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "agent-focus" }
$VenvDir = Join-Path $DataDir "venv"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
$VenvPyw = Join-Path $VenvDir "Scripts\pythonw.exe"
$TaskName = "AgentFocusDaemon"

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# 1. Python venv with the anki package. uv if present, else the py launcher, else python.
if (-not (Test-Path $VenvPy)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv venv --python 3.12 $VenvDir
        if ($LASTEXITCODE -ne 0) { uv venv $VenvDir }
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv $VenvDir
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        python -m venv $VenvDir
    } else {
        throw "Python 3.10 or newer is required. Install it from python.org or run: winget install Python.Python.3.12"
    }
}
if (-not (Test-Path $VenvPy)) { throw "Could not create the virtual environment in $VenvDir" }

$Requirements = Join-Path $RepoDir "requirements.txt"
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv pip install --python $VenvPy -r $Requirements
} else {
    & $VenvPy -m pip install --quiet -r $Requirements
}
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not install the anki package. Agent Focus will run without direct AnkiWeb sync."
}

# 2. Default config on first install. An existing config is never overwritten.
$Config = Join-Path $DataDir "config.json"
if (-not (Test-Path $Config)) {
    Copy-Item (Join-Path $RepoDir "config.example.json") $Config
    Write-Host "Created config at $Config"
}

# 3. Background daemon: a logon task running pythonw.exe (no console window), restarted on failure.
$Action = New-ScheduledTaskAction -Execute $VenvPyw -Argument "`"$(Join-Path $RepoDir 'server.py')`"" -WorkingDirectory $RepoDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Agent Focus daemon on http://127.0.0.1:7842" | Out-Null
Start-ScheduledTask -TaskName $TaskName

# 4. Agent hooks call hook_dispatch.py directly, so no bash is needed.
& $VenvPy (Join-Path $RepoDir "hooks\wire_hooks.py") install-direct $VenvPy (Join-Path $RepoDir "hooks\hook_dispatch.py")

# 5. CLI helper: agent-focus.cmd in the data folder, which is added to the user PATH.
$Cli = Join-Path $RepoDir "bin\agent_focus_cli.py"
Set-Content -Path (Join-Path $DataDir "agent-focus.cmd") -Encoding ASCII -Value "@`"$VenvPy`" `"$Cli`" %*"
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not (($UserPath -split ';') -contains $DataDir)) {
    $Parts = @($UserPath -split ';' | Where-Object { $_ }) + $DataDir
    [Environment]::SetEnvironmentVariable("Path", ($Parts -join ';'), "User")
    Write-Host "Added $DataDir to your PATH. Open a new terminal to use agent-focus."
}

Write-Host ""
Write-Host "Agent Focus installed and running at http://127.0.0.1:7842"
Write-Host "Try: agent-focus demo"
Write-Host "Sign in to AnkiWeb for flashcards: agent-focus anki login"
Write-Host "Load the browser extension from $(Join-Path $RepoDir 'extension') (README: Browser support)."
