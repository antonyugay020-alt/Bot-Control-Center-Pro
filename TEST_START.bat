@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 goto failed
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo First-time installation of the private Python environment...
    call "%~dp0INSTALL.bat"
    if errorlevel 1 goto failed
)
"%~dp0.venv\Scripts\python.exe" -c "import PySide6, requests, psutil" >nul 2>&1
if errorlevel 1 goto dependencies
if not exist "%~dp0TEST_DATA" mkdir "%~dp0TEST_DATA"
if errorlevel 1 goto failed
echo Bot Control Center Pro 17.0.9-W3-INTEGRATION-QA
echo PAPER ONLY. No exchange orders. Test accounts: TEST_DATA.
echo This console stays open while the GUI is running.
echo Startup log: TEST_DATA\ui_startup.log
"%~dp0.venv\Scripts\python.exe" -X faulthandler -u "%~dp0app.py" --data-root "%~dp0TEST_DATA" >"%~dp0TEST_DATA\ui_startup.log" 2>&1
set "BCC_TEST_EXIT=%ERRORLEVEL%"
echo Collecting PAPER test report (also after a GUI error)...
"%~dp0.venv\Scripts\python.exe" -m tools.export_test_report --root "%~dp0TEST_DATA" --ui-exit-code %BCC_TEST_EXIT%
if errorlevel 1 echo [REPORT ERROR] Use REPORT_NOW.bat to retry.
echo Reports directory: %~dp0TEST_DATA\REPORTS
if exist "%~dp0TEST_DATA\REPORTS" start "" explorer.exe "%~dp0TEST_DATA\REPORTS"
if not "%BCC_TEST_EXIT%"=="0" (
    echo [ERROR] GUI exited with status %BCC_TEST_EXIT%.
    echo ---------- ui_startup.log ----------
    type "%~dp0TEST_DATA\ui_startup.log"
    echo ------------------------------------
    pause
    exit /b %BCC_TEST_EXIT%
)
echo GUI closed normally. Test accounts remain in TEST_DATA.
exit /b 0
:dependencies
echo [ERROR] Python dependencies missing. Run INSTALL.bat and retry.
goto failed
:failed
echo Startup failed. Your existing MY_BOTS folder has not been changed.
pause
exit /b 1
