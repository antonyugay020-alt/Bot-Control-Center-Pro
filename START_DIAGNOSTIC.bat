@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"
if errorlevel 1 goto failed
if not exist "%~dp0.venv\Scripts\python.exe" (
    set "BCC_INSTALL_NO_PAUSE=1"
    call "%~dp0INSTALL.bat"
    if errorlevel 1 goto failed
)
set "LOGDIR=%LOCALAPPDATA%\BotControlCenterPro"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\startup_console.log"
echo [BCC] Running the real desktop GUI, with visible Python diagnostics.
echo [BCC] Log: "%LOG%"
echo [BCC] This console stays open until the GUI exits.
"%~dp0.venv\Scripts\python.exe" -X faulthandler -u "%~dp0launch.pyw" >"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo [BCC ERROR] GUI closed with exit code %RC%.
    type "%LOG%"
    echo [BCC] Full log: "%LOG%"
) else (
    echo [BCC] GUI closed normally.
)
pause
exit /b %RC%
:failed
echo [BCC ERROR] Desktop could not start; installation details are above.
pause
exit /b 1
