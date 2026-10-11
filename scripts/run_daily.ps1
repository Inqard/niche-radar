# Daily runner for Windows Task Scheduler (task "NicheRadar Daily", after Google's quota reset).
# Full output goes to data/logs/run-<timestamp>.log; the last lines are the report path and EXIT code.
$ErrorActionPreference = "Continue"
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project
New-Item -ItemType Directory -Force "$project\data\logs" | Out-Null
$log = "$project\data\logs\run-$(Get-Date -Format 'yyyy-MM-dd_HHmm').log"
# python from PATH, or set NICHE_RADAR_PYTHON to an explicit interpreter
$python = if ($env:NICHE_RADAR_PYTHON) { $env:NICHE_RADAR_PYTHON } else { (Get-Command python).Source }
& $python -u -m niche_radar run *> $log
Add-Content -Path $log -Value "EXIT=$LASTEXITCODE"
exit $LASTEXITCODE
