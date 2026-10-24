# 重启 ATM：先停掉正在运行的实例，再用最新源码拉起（与「启动 AI Teaches Me.vbs」行为一致）。
# 用法：pwsh -File scripts/restart_app.ps1 [-Wait]
param(
    [switch]$Wait,
    [int]$TimeoutSeconds = 40
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Get-AtmProcesses {
    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match '(-m\s+)?app[./\\]main' }
}

$stopped = @()
foreach ($proc in Get-AtmProcesses) {
    try {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
        $stopped += $proc.ProcessId
    } catch {
        Write-Warning "[atm] 没能停掉进程 $($proc.ProcessId)：$($_.Exception.Message)"
    }
}
if ($stopped.Count) {
    Write-Host "[atm] 已停止运行的实例：$($stopped -join ', ')"
} else {
    Write-Host "[atm] 没有正在运行的实例"
}

# 给系统一点时间释放窗口 / 文件句柄
Start-Sleep -Milliseconds 800

# 解释器选择与启动器保持一致：项目 .venv 优先，其次系统 Python
$venvPythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$pythonw = if (Test-Path $venvPythonw) { $venvPythonw } else { "pythonw.exe" }

Start-Process -FilePath $pythonw -ArgumentList "-m", "app.main" -WorkingDirectory $root
Write-Host "[atm] 已用 $pythonw 启动最新代码"

if (-not $Wait) { return }

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $now = @(Get-AtmProcesses)
    if ($now.Count -gt 0) {
        Write-Host "[atm] 新实例已就绪：PID $(($now | Select-Object -First 1).ProcessId)"
        return
    }
}
throw "[atm] 新实例没有起来（${TimeoutSeconds}s 内未检测到 app.main 进程）"
