@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 goto failed
rem Use exactly the same isolated Windows QA environment and evidence gate as CHECK_WINDOWS.bat.
rem This shortcut runs automated checks only; never modifies system Python packages.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start_windows_check.ps1" -AutomatedOnly
if errorlevel 1 goto failed
echo Automated Windows checks completed. Manual PAPER acceptance is still required.
pause
exit /b 0
:failed
echo Windows automated checks failed; inspect the BCC_VALIDATION_RESULTS report.
pause
exit /b 1
