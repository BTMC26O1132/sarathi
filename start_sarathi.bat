@echo off
setlocal
cd /d "%~dp0"
where powershell >nul 2>nul
if %errorlevel%==0 (
  powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/bootstrap' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
  if %errorlevel%==0 goto open_portal
)
where py >nul 2>nul
if %errorlevel%==0 (
  start "SARATHI local server" cmd /k py -3 server.py
  goto open_portal
)
where python >nul 2>nul
if %errorlevel%==0 (
  start "SARATHI local server" cmd /k python server.py
  goto open_portal
)
echo Python 3.10 or later was not found. Install Python and run this file again.
pause
exit /b 1
:open_portal
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8765"
endlocal

