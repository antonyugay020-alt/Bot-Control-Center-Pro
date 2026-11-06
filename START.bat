@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"
if errorlevel 1 goto failed
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"

rem The application is NOT the Codex development cycle. It does not require
rem Codex, Ollama, Git, API credentials or an AI workspace.
if not exist "%PY%" goto install
if not exist "%PYW%" goto install
"%PY%" -c "import PySide6, requests, psutil" >nul 2>&1
if errorlevel 1 goto install
 goto launch

:install
echo [BCC] Preparing the private Python/Qt runtime on first start...
set "BCC_INSTALL_NO_PAUSE=1"
call "%~dp0INSTALL.bat"
if errorlevel 1 goto failed
if not exist "%PYW%" goto failed
"%PY%" -c "import PySide6, requests, psutil" >nul 2>&1
if errorlevel 1 goto failed

:launch
rem Keep the desktop app consoleless; launch.pyw displays a native error dialog
rem and persists startup.log if Qt or the runtime service fails after spawning.
start "" "%PYW%" "%~dp0launch.pyw"
if errorlevel 1 goto failed
echo [BCC] Desktop application started. If the window does not appear,
echo [BCC] use START_DIAGNOSTIC.bat to see the actual Python traceback.
exit /b 0

:failed
echo.
echo [BCC ERROR] Desktop startup failed. No bots or PAPER accounts were reset.
echo [BCC ERROR] Run START_DIAGNOSTIC.bat to capture a detailed failure.
pause
exit /b 1
