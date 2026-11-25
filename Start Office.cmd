@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
  py -3 office.py
  if errorlevel 1 pause
  exit /b
)
where python >nul 2>&1
if not errorlevel 1 (
  python office.py
  if errorlevel 1 pause
  exit /b
)
echo Python 3.10+ is required for live mode.
echo Install from https://www.python.org/downloads/ and enable Add Python to PATH.
echo For the demo, open index.html in Chrome or Edge.
pause
