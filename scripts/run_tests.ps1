# Run all MoldBOM tests using the prebuilt Python env.
# Prebuilt env: C:\Users\littledark\.workbuddy\binaries\python\envs\default
# Falls back to `python` on PATH if not found.
$ErrorActionPreference = "Stop"

$py = "C:\Users\littledark\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $py = "python"
}

$root = Split-Path -Parent $PSScriptRoot
Write-Host "==> Using Python: $py"

Push-Location $root
try {
    & $py bom_export\test_bom_logic.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $py bom_export\test_stp_features.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $py V2\tests\test_engine.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $py V2\tests\test_namespec_infer.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $py V2\tests\test_keyword_op.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Push-Location V2
    try {
        & $py -m rulespec validate
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally {
        Pop-Location
    }

    Write-Host ""
    Write-Host "ALL TESTS PASSED"
} finally {
    Pop-Location
}