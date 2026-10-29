# Starts the trading bot in the background (no window), unless it is already running.
# Used by the Windows task "Quantara bot at logon"; safe to run by hand.
# Console output goes to logs\bot_console_<time>.out.txt / .err.txt; the main log is logs\quantara.log.
$root = Split-Path -Parent $PSScriptRoot
$running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*-m app.main run*" }
if ($running) {
    Add-Content "$root\logs\autostart.log" "$(Get-Date -Format s) bot already running (pid $($running[0].ProcessId)) - not starting another"
    exit 0
}
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$p = Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList "-m", "app.main", "run" `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput "$root\logs\bot_console_$stamp.out.txt" `
    -RedirectStandardError "$root\logs\bot_console_$stamp.err.txt"
Add-Content "$root\logs\autostart.log" "$(Get-Date -Format s) bot started (pid $($p.Id))"
