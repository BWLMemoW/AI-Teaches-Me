# 开发启动脚本（见《项目文档》§12）
# 用法：pwsh -File scripts/dev_run.ps1
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $python = $venvPython
    Write-Host "[dev] 使用虚拟环境 .venv"
} else {
    $python = "python"
    Write-Host "[dev] 未发现 .venv，使用系统 Python"
}

Push-Location $root
try {
    & $python -m app.main @Rest
} finally {
    Pop-Location
}
