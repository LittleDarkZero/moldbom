# Build BomExport.exe with an embedded auto-update token.
#
# Usage:
#   .\scripts\build_exe.ps1                       # no embedded token (public repo)
#   .\scripts\build_exe.ps1 -Token ghp_xxxx       # embed token
#   $env:MOLDBOM_TOKEN = "ghp_xxxx"; .\scripts\build_exe.ps1
#
# Security: the token is written into bom_export\bom_token.py only during
# the build (compiled INTO BomExport.exe by PyInstaller), then the original
# file is restored byte-for-byte. No update_config.json is shipped - all
# auto-update settings (repo/token) live inside the exe.
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
# byte-level backup (encoding-agnostic, immune to PS 5.1 codepage issues)
$originalBytes = [System.IO.File]::ReadAllBytes($tokenFile)

try {
    if ($Token) {
        # inject token (escape single quotes)
        $escaped = $Token.Replace("'", "''")
        $content = "# -*- coding: utf-8 -*-`nEMBEDDED_TOKEN = '$escaped'`n"
        [System.IO.File]::WriteAllBytes($tokenFile, [System.Text.Encoding]::UTF8.GetBytes($content))
        # verify injection BEFORE PyInstaller compiles it
        $check = [System.IO.File]::ReadAllText($tokenFile, [System.Text.Encoding]::UTF8)
        if (-not $check.Contains($Token)) {
            throw "token injection verification FAILED: token not found in bom_token.py"
        }
        Write-Host "==> Embedded token injected and verified (len=$($Token.Length))"
    } else {
        Write-Host "==> No token provided; building WITHOUT embedded token"
    }

    Push-Location (Join-Path $root "bom_export")
    try {
        & $py -m PyInstaller --clean --noconfirm BomExport.spec
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        # no update_config.json is shipped: purge any stale one from dist
        Remove-Item dist\update_config.json -Force -ErrorAction SilentlyContinue
        Write-Host "==> Built: $((Get-Item dist\BomExport.exe).FullName)"
    } finally {
        Pop-Location
    }
} finally {
    # byte-level restore of the original bom_token.py
    [System.IO.File]::WriteAllBytes($tokenFile, $originalBytes)
    $cur = [System.IO.File]::ReadAllBytes($tokenFile)
    if ($cur.Length -ne $originalBytes.Length) {
        throw "restore verification FAILED: length mismatch"
    }
    for ($i = 0; $i -lt $originalBytes.Length; $i++) {
        if ($cur[$i] -ne $originalBytes[$i]) {
            throw "restore verification FAILED: byte mismatch at $i"
        }
    }
    if ($Token -and ([System.Text.Encoding]::UTF8.GetString($cur)).Contains($Token)) {
        throw "restore verification FAILED: token still present after restore"
    }
    Write-Host "==> bom_token.py restored (byte-identical)"
}
