param(
    [string]$ProjectRoot = "C:\Users\Home\Desktop\Flow Bot AI - Trader AI Agent",
    [string]$DeviceId = "",
    [string]$DataDir = "",
    [string]$LogsDir = "",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8787,
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path $ProjectRoot).Path
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
$pythonExe = (& python -c "import sys; print(sys.executable)") | Select-Object -Last 1

if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    throw "Unable to resolve a Python interpreter."
}

if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $env:PYTHONPATH = $root
}
elseif (-not $env:PYTHONPATH.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
    $env:PYTHONPATH = "$root;$($env:PYTHONPATH)"
}

$args = @(
    "-m", "daytrading_bot.cli", "serve-dashboard-app",
    "--data-dir", $DataDir,
    "--logs-dir", $LogsDir,
    "--host", $BindHost,
    "--port", [string]$Port
)

if ($OpenBrowser) {
    $args += "--open-browser"
}

& $pythonExe @args
