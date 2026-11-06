@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
if errorlevel 1 (
  echo [BCC AI] Cannot open the BCC directory.
  goto :failed_no_log
)
if not exist ".ai_work" mkdir ".ai_work"
if not exist ".ai_work" (
  echo [BCC AI] Cannot create the .ai_work log directory.
  goto :failed_no_log
)
set "BCC_AI_LAUNCH_LOG=%CD%\.ai_work\AI_CYCLE_WINDOWS_LAST.log"
>"%BCC_AI_LAUNCH_LOG%" echo [BCC AI] CYCLE launcher started.
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [BCC AI] Python environment is missing: .venv\Scripts\python.exe
  echo [BCC AI] Run INSTALL.bat in this folder before AI_CYCLE_WINDOWS.bat
  >>"%BCC_AI_LAUNCH_LOG%" echo ERROR: .venv\Scripts\python.exe is missing. Run INSTALL.bat first.
  set "RC=2"
  goto :finish
)
echo [BCC AI] This is an optional SOURCE DEVELOPMENT command, NOT the desktop program.
echo [BCC AI] To open the graphical application, run START.bat.
echo [BCC AI] Running CYCLE. Do not close this window while it works.
echo [BCC AI] Log: "%BCC_AI_LAUNCH_LOG%"
"%PY%" -u -m tools.windows_ai_launcher cycle
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [BCC AI] BLOCKED. Exit code %RC%.
  echo [BCC AI] Read the message above or the log file.
  >>"%BCC_AI_LAUNCH_LOG%" echo BLOCKED with exit code %RC%.
) else (
  echo.
  echo [BCC AI] Finished. Check the saved log and validation status.
)
goto :finish
:failed_no_log
set "RC=2"
:finish
echo.
echo [BCC AI] Window remains open so you can read the result.
if /I "%BCC_AI_NO_PAUSE%"=="1" exit /b %RC%
pause
exit /b %RC%
