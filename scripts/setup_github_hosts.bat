@echo off
setlocal
REM ============================================================
REM  MoldBOM GitHub update acceleration - optional hosts entries
REM  Run as Administrator (right-click -> Run as administrator).
REM  Add:    double-click (or: setup_github_hosts.bat)
REM  Undo:   setup_github_hosts.bat undo
REM
REM  This only helps networks where GitHub DNS is polluted/blocked
REM  at DNS level. It does NOT help IP-level blocking, and GitHub
REM  IPs change over time - if updates still fail, edit the IPs
REM  below (or prefer the built-in Gitee mirror instead).
REM ============================================================

set "HOSTS=%SystemRoot%\System32\drivers\etc\hosts"
set "BAK=%HOSTS%.moldbom.bak"
set "MARK=MoldBOM GitHub accel"

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Please run as Administrator.
    pause
    exit /b 1
)

if /i "%~1"=="undo" goto undo
if /i "%~1"=="-u" goto undo

findstr /c:"%MARK%" "%HOSTS%" >nul 2>&1
if not errorlevel 1 (
    echo [SKIP] Acceleration block already exists in hosts.
    goto done
)

if not exist "%BAK%" copy /y "%HOSTS%" "%BAK%" >nul

>>"%HOSTS%" (
    echo.
    echo # === MoldBOM GitHub accel BEGIN ===
    echo 20.205.243.168 api.github.com
    echo 185.199.108.133 raw.githubusercontent.com
    echo 185.199.108.133 release-assets.githubusercontent.com
    echo 185.199.108.133 objects.githubusercontent.com
    echo # === MoldBOM GitHub accel END ===
)
echo [OK] Acceleration block added to hosts.
echo       api.github.com                    - update manifest / asset API
echo       raw.githubusercontent.com         - fallback manifest
echo       release-assets.githubusercontent.com - asset download CDN
echo       objects.githubusercontent.com     - asset storage
echo [TIP] GitHub IPs may change. If updates still fail, undo, edit this
echo       script with fresh IPs, then re-run.
goto done

:undo
if not exist "%BAK%" (
    echo [SKIP] No backup found, nothing to undo.
    goto done
)
copy /y "%BAK%" "%HOSTS%" >nul
echo [OK] Hosts restored from backup.

:done
ipconfig /flushdns >nul 2>&1
echo [OK] DNS cache flushed.
pause
