# Build BomExport.exe with an embedded auto-update token.
#
# Usage:
#   .\scripts\build_exe.ps1                       # no embedded token (public repo)
#   .\scripts\build_exe.ps1 -Token ghp_xxxx       # embed token
#   $env:MOLDBOM_TOKEN = "ghp_xxxx"; .\scripts\build_exe.ps1
#
# Security: the token is written into bom_export\bom_token.py only during
# the build and restored to empty afterwards (finally block). Never commit
# a real token to the repository.
param(
    [string]$Token = $env:MOLDBOM_TOKEN
)

$ErrorActionPreference = "Stop"

$py = "C:\Users\littledark\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $py = "python"
}

$root = Split-Path -Parent $PSScriptRoot
$tokenFile = Join-Path $root "bom_export\bom_token.py"
$original = Get-Content $tokenFile -Raw -Encoding UTF8

try {
    if ($Token) {
        # 注入 token（转义单引号）
        $escaped = $Token.Replace("'", "''")
        $content = "# -*- coding: utf-8 -*-`nEMBEDDED_TOKEN = '$escaped'`n"
        [System.IO.File]::WriteAllText($tokenFile, $content, (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "==> Embedded token injected (len=$($Token.Length))"
    } else {
        Write-Host "==> No token provided; building WITHOUT embedded token"
    }

    Push-Location (Join-Path $root "bom_export")
    try {
        & $py -m PyInstaller --clean --noconfirm BomExport.spec
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Copy-Item update_config.example.json dist\update_config.json -Force
        Write-Host "==> Built: $((Get-Item dist\BomExport.exe).FullName)"
    } finally {
        Pop-Location
    }
} finally {
    # 恢复为空模板，防止真实 token 残留/误提交
    [System.IO.File]::WriteAllText($tokenFile, $original, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "==> bom_token.py restored"
}
