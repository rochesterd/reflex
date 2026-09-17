<#
    Registers "Reflex buffer cleanup": clear_reflex_buffer.ps1 (beside this
    file), as SYSTEM, at every logon. Run by reflex.iss at install time.

    A script rather than a schtasks.exe line because schtasks cannot say
    the one thing this task most needs: run on battery. Its defaults are
    "don't start on batteries, stop when going onto them", and the case
    this task exists for is a crash or a *power cut* -- exactly when a
    laptop kiosk is on battery at its next logon. Found on the first clinic
    install, 2026-09-17; see DECISIONS.md.

    -DefinitionOnly builds the task and prints its settings without
    registering anything, so the definition can be checked unelevated.
#>
param([switch]$DefinitionOnly)

$ErrorActionPreference = 'Stop'

$sweep = Join-Path $PSScriptRoot 'clear_reflex_buffer.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$sweep`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
# The sweep takes seconds; the default limit is three days.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings

if ($DefinitionOnly) {
    $task.Actions | Format-List Execute, Arguments
    $task.Settings | Format-List DisallowStartIfOnBatteries, StopIfGoingOnBatteries, ExecutionTimeLimit, Enabled
    $task.Principal | Format-List UserId, LogonType, RunLevel
    exit 0
}

Register-ScheduledTask -TaskName 'Reflex buffer cleanup' -InputObject $task -Force | Out-Null
exit 0
