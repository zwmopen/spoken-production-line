param([string]$Python = "python", [string]$TaskName = "SpokenProductionStatus")
$ErrorActionPreference = "Stop"
$bundleRoot = $PSScriptRoot
$pythonExe = (Get-Command $Python).Source
$taskRoot = Join-Path $bundleRoot "data\tasks"
$entry = Join-Path $bundleRoot "skills\spoken-production-line\scripts\pipeline.py"
$action = New-ScheduledTaskAction -Execute $pythonExe -Argument ('-X utf8 "' + $entry + '" status --task-root "' + $taskRoot + '"') -WorkingDirectory $bundleRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Local status inventory only; does not submit production." -Force
