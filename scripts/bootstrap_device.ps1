param(
    [string]$ProjectRoot = "C:\Users\Home\Desktop\Flow Bot AI - Trader AI Agent",
    [string]$DeviceId = "",
    [string]$DesktopDir = "",
    [switch]$MigrateLegacy,
    [switch]$MoveLegacy
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path $ProjectRoot).Path
$pythonExe = (& python -c "import sys; print(sys.executable)") | Select-Object -Last 1

if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    throw "Unable to resolve a Python interpreter."
}

$args = @(
    "-m", "daytrading_bot.cli", "bootstrap-device",
    "--project-root", $root
)

if (-not [string]::IsNullOrWhiteSpace($DeviceId)) {
    $args += @("--device-id", $DeviceId)
}
if (-not [string]::IsNullOrWhiteSpace($DesktopDir)) {
    $args += @("--desktop-dir", $DesktopDir)
}
if ($MigrateLegacy) {
    $args += "--migrate-legacy"
}
if ($MoveLegacy) {
    $args += "--move-legacy"
}

& $pythonExe @args
