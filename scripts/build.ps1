# 打包脚本（见《项目文档》§13.8，M6 交付）
# 用法：pwsh -File scripts/build.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Push-Location $root
try {
    python -m PyInstaller --noconfirm --windowed --name "AI-Teaches-Me" `
        --add-data "app/web;app/web" `
        --add-data "prompts;prompts" `
        app/main.py
} finally {
    Pop-Location
}
