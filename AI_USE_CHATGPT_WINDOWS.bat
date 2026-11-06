@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
 echo [BCC AI] Run INSTALL.bat first.
 set "RC=2"
 goto :finish
)
"%PY%" -u -m tools.ai_dev_pipeline use-chatgpt
set "RC=%ERRORLEVEL%"
:finish
if not "%RC%"=="0" echo [BCC AI] BLOCKED, exit=%RC%
echo [BCC AI] This does not certify a BCC release.
if /I "%BCC_AI_NO_PAUSE%"=="1" exit /b %RC%
pause
exit /b %RC%
