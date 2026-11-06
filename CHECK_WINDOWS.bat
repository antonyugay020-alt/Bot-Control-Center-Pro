@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 goto path_error
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start_windows_check.ps1"
set "BCC_CHECK_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %BCC_CHECK_EXIT%
:path_error
echo Cannot open the package folder. Extract the ZIP to a local folder first.
pause
exit /b 1
