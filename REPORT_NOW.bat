@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [ERROR] Python environment missing. Run INSTALL.bat.
    pause
    exit /b 1
)
echo Collecting diagnostic ZIP from TEST_DATA without stopping bots...
"%~dp0.venv\Scripts\python.exe" -m tools.export_test_report --root "%~dp0TEST_DATA"
if errorlevel 1 (
    echo [ERROR] Could not create a report. Check output above.
    pause
    exit /b 1
)
echo Open TEST_DATA\REPORTS and send the newest BCC_TEST_REPORT_*.zip.
pause
exit /b 0
