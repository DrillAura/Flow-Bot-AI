param(
    [string]$ProjectRoot = "C:\Users\Home\Desktop\Flow Bot AI - Trader AI Agent",
    [string]$DeviceId = "",
    [string]$DataDir = "",
    [string]$LogsDir = "",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8787,
    [int]$StartupTimeoutSeconds = 20
)

Add-Type -AssemblyName System.Windows.Forms
$ErrorActionPreference = "Stop"

function Show-ErrorDialog {
    param([string]$Message)
    [void][System.Windows.Forms.MessageBox]::Show($Message, "Flow Bot Dashboard", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
}

function Write-LauncherLog {
    param([string]$Message)
    try {
        $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $script:LogPath -Value ("[{0}] {1}" -f $timestamp, $Message) -Encoding utf8
    }
    catch {
    }
}

function Test-DashboardHealth {
    param([string]$Url)
    try {
        $response = Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 2
        return $response.ok -eq $true
    }
    catch {
        return $false
    }
}

try {
    $resolvedRoot = (Resolve-Path $ProjectRoot).Path
}
catch {
    Show-ErrorDialog "Flow Bot project root was not found.`n`nExpected:`n$ProjectRoot"
    exit 1
}

$resolvedDeviceId = $DeviceId
if ([string]::IsNullOrWhiteSpace($resolvedDeviceId)) {
    $resolvedDeviceId = if (-not [string]::IsNullOrWhiteSpace($env:FLOW_BOT_DEVICE_ID)) { $env:FLOW_BOT_DEVICE_ID } elseif (-not [string]::IsNullOrWhiteSpace($env:COMPUTERNAME)) { $env:COMPUTERNAME } else { "default-device" }
}
$resolvedDeviceId = (($resolvedDeviceId.ToLowerInvariant()) -replace '[^a-z0-9._-]+', '-').Trim('-','_','.')
if ([string]::IsNullOrWhiteSpace($resolvedDeviceId)) {
    $resolvedDeviceId = "default-device"
}
if ([string]::IsNullOrWhiteSpace($DataDir)) {
    $DataDir = ".runtime\$resolvedDeviceId\data"
}
if ([string]::IsNullOrWhiteSpace($LogsDir)) {
    $LogsDir = ".runtime\$resolvedDeviceId\logs\ops"
}

$opsDir = Join-Path $resolvedRoot $LogsDir
New-Item -ItemType Directory -Force -Path $opsDir | Out-Null
$script:LogPath = Join-Path $opsDir "desktop_dashboard_launcher.log"
Write-LauncherLog "Launcher started."
Write-LauncherLog "ProjectRoot=$resolvedRoot"
Write-LauncherLog "DeviceId=$resolvedDeviceId"

$pythonExe = (& python -c "import sys; print(sys.executable)") | Select-Object -Last 1
if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    Show-ErrorDialog "Python interpreter could not be resolved."
    exit 1
}

$pythonDir = Split-Path -Parent $pythonExe
$pythonwExe = Join-Path $pythonDir "pythonw.exe"
if (-not (Test-Path $pythonwExe)) {
    $pythonwExe = $pythonExe
}

$healthUrl = "http://$BindHost`:$Port/healthz"
$dashboardUrl = "http://$BindHost`:$Port/"

if (-not (Test-DashboardHealth $healthUrl)) {
    Write-LauncherLog "No running dashboard found on $dashboardUrl. Starting pythonw background server."
    $command = 'set "PYTHONPATH={0};%PYTHONPATH%" && "{1}" -m daytrading_bot.cli serve-dashboard-app --data-dir "{2}" --logs-dir "{3}" --host {4} --port {5}' -f $resolvedRoot, $pythonwExe, $DataDir, $LogsDir, $BindHost, $Port
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $command -WorkingDirectory $resolvedRoot -WindowStyle Hidden | Out-Null

    $ready = $false
    for ($i = 0; $i -lt $StartupTimeoutSeconds; $i++) {
        Start-Sleep -Seconds 1
        if (Test-DashboardHealth $healthUrl) {
            $ready = $true
            break
        }
    }

    if (-not $ready) {
        Write-LauncherLog "Dashboard server did not become healthy within timeout."
        Show-ErrorDialog "The dashboard server did not become ready in time.`n`nCheck:`n$script:LogPath"
        exit 1
    }
}
else {
    Write-LauncherLog "Dashboard already running. Reusing existing server."
}

Write-LauncherLog "Opening browser at $dashboardUrl"
Start-Process $dashboardUrl | Out-Null
exit 0
