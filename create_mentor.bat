@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py --create-mentor
  goto done
)
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py --create-mentor
  goto done
)
echo Python 3.10 or later was not found. Install Python and run this file again.
:done
pause
endlocal

