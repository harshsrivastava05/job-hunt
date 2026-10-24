# Runs the job hunter at every Windows login: hidden, low priority, back within 10 minutes if it dies. Starts it now,
# adds "Job Hunt" shortcuts (desktop + Start menu) that start it if needed and open the queue, and names its notifications.
# Usage: powershell -ExecutionPolicy Bypass -File install-windows.ps1 [-Uninstall]
param([switch]$Uninstall)
$name = "Job Hunt"
$shortcuts = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Job Hunt.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Job Hunt.lnk")
)
$aumid = "HKCU:\Software\Classes\AppUserModelId\JobHunt.Watcher"  # notifications show as "Job Hunt"
Remove-Item (Join-Path ([Environment]::GetFolderPath("Desktop")) "Job Hunt.url") -ErrorAction SilentlyContinue  # older version's link

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    $shortcuts | ForEach-Object { Remove-Item $_ -ErrorAction SilentlyContinue }
    Remove-Item $aumid -Recurse -ErrorAction SilentlyContinue
    "Removed. Your data stays in $HOME\.jobhunt"
    return
}

$pythonw = "$PSScriptRoot\.venv\Scripts\pythonw.exe"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m jobhunt.app" -WorkingDirectory $PSScriptRoot
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# Task Scheduler's "restart on failure" doesn't cover a program that dies after starting, so it is also launched
# every 10 minutes: while it runs those launches are ignored (IgnoreNew), and if it died it comes back
$every10 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10)
# no time limit (the default kills tasks after 3 days), runs on battery; priority stays below normal
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $name -Action $action -Trigger $atLogon, $every10 -Settings $settings -Force `
    -Description "Watches career pages for new jobs and queues the good ones: http://127.0.0.1:8000" | Out-Null

New-Item -Path $aumid -Force | Out-Null
New-ItemProperty -Path $aumid -Name DisplayName -Value "Job Hunt" -PropertyType String -Force | Out-Null

$shell = New-Object -ComObject WScript.Shell
foreach ($path in $shortcuts) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "-m jobhunt.app --open"
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.IconLocation = "$env:SystemRoot\System32\imageres.dll,-1023"
    $lnk.Description = "Open the job queue (starts the watcher if it isn't running)"
    $lnk.Save()
}

Start-ScheduledTask -TaskName $name
"Installed and running. Queue: http://127.0.0.1:8000 (or the 'Job Hunt' shortcut). Log: $HOME\.jobhunt\jobhunt.log"
