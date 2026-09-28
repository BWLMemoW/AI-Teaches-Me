# 打包脚本（《项目文档》§13.8 / §12；M6 交付物）
# 用法：pwsh -File scripts/build.ps1
param(
    [string]$Name = "AI-Teaches-Me",
    [switch]$OneDir
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    Write-Host "[build] 使用 PyInstaller 打包 $Name ..."
    $mode = if ($OneDir) { "--onedir" } else { "--onefile" }
    python -m PyInstaller --noconfirm --windowed --name $Name $mode `
        --add-data "app/web;app/web" `
        --add-data "app/ui/assets;app/ui/assets" `
        --add-data "prompts;prompts" `
        app/main.py
    Write-Host "[build] 完成，产物在 dist/$Name"
}
finally {
    Pop-Location
}
