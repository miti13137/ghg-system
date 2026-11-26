@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -X utf8 install_windows.py
) else (
  python -X utf8 install_windows.py
)
set "install_result=%ERRORLEVEL%"
pause
exit /b %install_result%
