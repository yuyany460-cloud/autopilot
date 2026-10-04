# PTA 单选题任务
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File D:\code\autopilot\examples\pta-exam.ps1
#
# 任务说明写在 examples\pta-task.txt 里（长文本放文件，避免命令行转义问题）。
# 想换题目集，改 pta-task.txt 里的网址即可。

$ErrorActionPreference = "Stop"
Set-Location "D:\code\autopilot"

$taskFile = "examples\pta-task.txt"

if (-not (Test-Path $taskFile)) {
    Write-Host "找不到任务文件：$taskFile" -ForegroundColor Red
    exit 1
}

Write-Host "任务文件：$taskFile" -ForegroundColor Cyan
Get-Content $taskFile -Encoding UTF8 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
Write-Host ""

python -m autopilot run --goal-file $taskFile --max-steps 60

Write-Host ""
Write-Host "审计日志：D:\code\autopilot\var\logs" -ForegroundColor DarkGray
