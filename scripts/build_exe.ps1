# Build BomExport.exe (public repo - no embedded token needed).
#
# Usage:
#   .\scripts\build_exe.ps1
#
# No update_config.json is shipped. All auto-update settings (GitHub repo,
# Gitee mirror, check interval, channel) are compiled into the exe.
# Runtime state (last_check / auto_check) is stored in %APPDATA%\MoldBOM.
param()

$ErrorActionPreference = "Stop"

$py = "C:\Users\littledark\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $py = "python"
}

$root = Split-Path -Parent $PSScriptRoot

Push-Location (Join-Path $root "bom_export")
try {
    & $py -m PyInstaller --clean --noconfirm BomExport.spec
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    # no update_config.json is shipped: purge any stale one from dist
    Remove-Item (Join-Path (Get-Location) "dist\update_config.json") -Force -ErrorAction SilentlyContinue

    $exe = Get-Item (Join-Path (Get-Location) "dist\BomExport.exe")
    Write-Host "==> Built: $($exe.FullName)"
    Write-Host "==> Size: $($exe.Length) bytes"
} finally {
    Pop-Location
}