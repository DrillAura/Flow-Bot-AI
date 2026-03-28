param(
    [string]$TaskName = "FlowBotSupervisorWatchdog",
    [string]$ProjectRoot = "C:\Users\Home\Desktop\Flow Bot AI - Trader AI Agent",
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path $ProjectRoot).Path
$scriptPath = Join-Path $root "scripts\start_supervisor_watchdog.ps1"
$cmdWrapperPath = Join-Path $root "scripts\run_supervisor_watchdog.cmd"

$registeredWith = ""
try {
    $actionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -ProjectRoot `"$root`""
    if (-not [string]::IsNullOrWhiteSpace($DataDir)) {
        $actionArgs += " -DataDir `"$DataDir`""
    }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArgs
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Keeps the Flow Bot supervisor watchdog running after logon and restarts on failure." -Force | Out-Null
    $registeredWith = "Register-ScheduledTask"
}
catch {
    $taskCmd = '"' + $cmdWrapperPath + '"'
    schtasks /Create /F /SC ONLOGON /TN $TaskName /TR $taskCmd /RL LIMITED | Out-Null
    $registeredWith = "schtasks"
}

[pscustomobject]@{
    task_name = $TaskName
    project_root = $root
    script_path = $scriptPath
    cmd_wrapper_path = $cmdWrapperPath
    status = "registered"
    registered_with = $registeredWith
} | ConvertTo-Json -Depth 3
