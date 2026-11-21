param(
    [ValidateRange(1024,65535)][int]$DebugPort = 54732,
    [string]$ProfileDirectory = (Join-Path $env:LOCALAPPDATA 'CodexRewindDesktop\desktop-profile'),
    [string]$CodexDataDirectory = $(if ($env:CODEX_HOME) {$env:CODEX_HOME} else {Join-Path $env:USERPROFILE '.codex'}),
    [string]$DataDirectory = (Join-Path $env:LOCALAPPDATA 'CodexRewindDesktop'),
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $DataDirectory -Force | Out-Null
$stopFile = Join-Path $DataDirectory 'native.stop'
if ($Stop) {
    [System.IO.File]::WriteAllText($stopFile, 'stop')
    Write-Output 'Native controls stopping; snapshots retained.'
    exit 0
}
$node = Get-Command node -ErrorAction Stop
$python = Get-Command python -ErrorAction Stop
$nodeMajor = [int]((& $node.Source --version) -replace '^v(\d+).*','$1')
if ($nodeMajor -lt 22) {throw 'Node.js 22 or later is required'}
$connectionFile = Join-Path $DataDirectory 'native-connection.json'
$hostRunning = $false
if (Test-Path -LiteralPath $connectionFile) {
    $existing = Get-Content -LiteralPath $connectionFile -Raw | ConvertFrom-Json
    $owner = if ($existing.pid) {Get-Process -Id $existing.pid -ErrorAction SilentlyContinue} else {$null}
    if ($owner) {$hostRunning = $true}
}
$package = Get-AppxPackage -Name OpenAI.Codex | Select-Object -First 1
if (-not $package) { throw 'OpenAI.Codex package not registered' }
$appExe = Join-Path $package.InstallLocation 'app\ChatGPT.exe'
if (-not (Test-Path -LiteralPath $appExe -PathType Leaf)) { throw 'Desktop executable missing' }
$profilePath = [System.IO.Path]::GetFullPath($ProfileDirectory)
$dataPath = [System.IO.Path]::GetFullPath($CodexDataDirectory)
if (-not (Test-Path -LiteralPath $dataPath -PathType Container)) { throw 'CODEX_HOME must already exist' }
foreach ($value in @($appExe, $profilePath, $dataPath)) {
    if ($value.Contains('"') -or $value.Contains("`r") -or $value.Contains("`n") -or $value.Contains('%')) {
        throw 'Unsupported wrapper path character'
    }
}
New-Item -ItemType Directory -Path $profilePath -Force | Out-Null
$wrapper = Join-Path $profilePath 'launch-in-package.cmd'
$appDirectory = Split-Path -Parent $appExe
$script = @"
@echo off
setlocal
set "ELECTRON_RUN_AS_NODE="
set "ELECTRON_NO_ASAR="
set "NODE_OPTIONS="
set "CODEX_HOME=$dataPath"
set "CODEX_ELECTRON_USER_DATA_PATH=$profilePath"
cd /d "$appDirectory"
start "" /b "$appExe" --remote-debugging-port=$DebugPort --remote-debugging-address=127.0.0.1 --remote-allow-origins=http://127.0.0.1:$DebugPort --user-data-dir="$profilePath"
endlocal
"@
# Paths used by this workspace are ASCII. Avoid cmd.exe interpreting a UTF-8 BOM.
if ($script.ToCharArray() | Where-Object { [int]$_ -gt 127 }) { throw 'Use ASCII profile and Codex paths for this research wrapper' }
[System.IO.File]::WriteAllText($wrapper, ($script -replace "`r?`n", "`r`n"), [System.Text.Encoding]::ASCII)
Write-Output "PACKAGE_FAMILY=$($package.PackageFamilyName)"
Write-Output "WRAPPER=$wrapper"
Invoke-CommandInDesktopPackage -PackageFamilyName $package.PackageFamilyName -AppId 'App' -Command 'cmd.exe' -Args ('/d /c "' + $wrapper + '"') -PreventBreakaway
# Keep the PowerShell host alive through Electron startup. This avoids a known
# packaged-launch race seen in the referenced project. This script does not stop
# or restart the existing main Codex process.
$deadline = [DateTime]::UtcNow.AddSeconds(30)
$ready = $false
while (-not $ready -and [DateTime]::UtcNow -lt $deadline) {
    $client = New-Object System.Net.Sockets.TcpClient
    try { $ready = $client.ConnectAsync('127.0.0.1', $DebugPort).Wait(500) -and $client.Connected }
    catch { }
    finally { $client.Close() }
    if (-not $ready) { Start-Sleep -Milliseconds 250 }
}
Write-Output "PORT_READY=$([int]$ready)"
if (-not $ready) { exit 1 }
if ($hostRunning) {Write-Output 'Enhanced desktop opened; native rewind host already running.'; exit 0}
$hostScript = Join-Path $PSScriptRoot 'native_host.py'
$hostArgs = '"' + $hostScript + '" --port ' + $DebugPort + ' --data-dir "' + $DataDirectory + '"'
Start-Process -FilePath $python.Source -ArgumentList $hostArgs -WindowStyle Hidden -RedirectStandardOutput (Join-Path $DataDirectory 'native.stdout.log') -RedirectStandardError (Join-Path $DataDirectory 'native.stderr.log') | Out-Null
Write-Output 'Enhanced desktop started. Open a local chat to use the reply rewind icon.'
