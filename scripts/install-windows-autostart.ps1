# Автозапуск бота після входу в Windows (ПК має бути увімкнений)
$taskName = "FindHomeBot"
$bat = Join-Path (Split-Path $PSScriptRoot -Parent) "start-bot.bat"
$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory (Split-Path $bat -Parent)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force
Write-Host "OK: задача '$taskName' — бот стартує після входу в Windows."
