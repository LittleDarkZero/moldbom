# Sync GitHub main -> Gitee mirror (run on a machine that has Gitee credentials).
# Non-interactive: uses your existing Gitee credential, no login popup.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\sync_gitee.ps1
param()

$ErrorActionPreference = "Stop"

$remotes = git remote
if ($remotes -notcontains "gitee") {
    git remote add gitee https://gitee.com/LittleDarkZero/moldbom.git
    Write-Host "==> Added remote: gitee"
}

Write-Host "==> Pulling latest from GitHub (origin/main)..."
git pull origin main
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Pushing to Gitee (gitee/main)..."
git push gitee main
exit $LASTEXITCODE