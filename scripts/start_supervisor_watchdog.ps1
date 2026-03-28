param(
    [string]$ProjectRoot = "C:\Users\Home\Desktop\Flow Bot AI - Trader AI Agent",
    [string]$DeviceId = "",
    [string]$DataDir = "",
    [string]$OpsLogsDir = "",
    [string]$RunId = "",
    [int]$WatchdogPollSeconds = 60,
    [int]$CapturePollSeconds = 60,
    [int]$SupervisorPollSeconds = 300,
    [bool]$EnableResearchScans = $true,
    [double]$ResearchScanAvailableEur = 100.0,
    [int]$ResearchScanDurationSeconds = 90,
    [int]$ResearchScanMaxMessages = 60,
    [int]$ResearchScanMinIntervalSeconds = 900
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RunId)) {
    $RunId = Get-Date -Format "yyyyMMdd_HHmmss"
}

$root = (Resolve-Path $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DeviceId)) {
    $DeviceId = if (-not [string]::IsNullOrWhiteSpace($env:FLOW_BOT_DEVICE_ID)) { $env:FLOW_BOT_DEVICE_ID } elseif (-not [string]::IsNullOrWhiteSpace($env:COMPUTERNAME)) { $env:COMPUTERNAME } else { "default-device" }
}
$DeviceId = (($DeviceId.ToLowerInvariant()) -replace '[^a-z0-9._-]+', '-').Trim('-','_','.')
if ([string]::IsNullOrWhiteSpace($DeviceId)) {
    $DeviceId = "default-device"
}
if ([string]::IsNullOrWhiteSpace($DataDir)) {
    $DataDir = ".runtime\$DeviceId\data"
}
if ([string]::IsNullOrWhiteSpace($OpsLogsDir)) {
    $OpsLogsDir = ".runtime\$DeviceId\logs\ops"
}
$runDir = Join-Path $root (Join-Path $OpsLogsDir ("supervisor_watchdog_" + $RunId))
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

$statePath = Join-Path $runDir "supervisor_state.json"
$stdoutPath = Join-Path $runDir "watchdog_stdout.log"
$stderrPath = Join-Path $runDir "watchdog_stderr.log"
$supervisorStdoutPath = Join-Path $runDir "supervisor_stdout.log"
$supervisorStderrPath = Join-Path $runDir "supervisor_stderr.log"
$paperForwardStdoutPath = Join-Path $runDir "paper_forward_stdout.log"
$paperForwardStderrPath = Join-Path $runDir "paper_forward_stderr.log"
$pythonExe = (& python -c "import sys; print(sys.executable)") | Select-Object -Last 1

if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    throw "Unable to resolve a Python interpreter."
}

$argLine = @(
    "-m daytrading_bot.cli supervisor-watchdog",
    "--data-dir `"$DataDir`"",
    "--state-path `"$statePath`"",
    "--watchdog-poll-seconds $WatchdogPollSeconds",
    "--capture-poll-seconds $CapturePollSeconds",
    "--supervisor-poll-seconds $SupervisorPollSeconds",
    "--setup both",
    "--profile fast",
    "--objective hybrid",
    "--train-days 10",
    "--test-days 3",
    "--top 3",
    "--supervisor-stdout-path `"$supervisorStdoutPath`"",
    "--supervisor-stderr-path `"$supervisorStderrPath`"",
    "--paper-forward-stdout-path `"$paperForwardStdoutPath`"",
    "--paper-forward-stderr-path `"$paperForwardStderrPath`""
) -join " "

if ($EnableResearchScans) {
    $argLine += " --enable-research-scans"
    $argLine += " --research-scan-available-eur $ResearchScanAvailableEur"
    $argLine += " --research-scan-duration-seconds $ResearchScanDurationSeconds"
    $argLine += " --research-scan-max-messages $ResearchScanMaxMessages"
    $argLine += " --research-scan-min-interval-seconds $ResearchScanMinIntervalSeconds"
}

$proc = Start-Process -FilePath $pythonExe -ArgumentList $argLine -WorkingDirectory $root -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru

[pscustomobject]@{
    watchdog_pid = $proc.Id
    device_id = $DeviceId
    run_dir = $runDir
    state_path = $statePath
    stdout_path = $stdoutPath
    stderr_path = $stderrPath
} | ConvertTo-Json -Depth 3
