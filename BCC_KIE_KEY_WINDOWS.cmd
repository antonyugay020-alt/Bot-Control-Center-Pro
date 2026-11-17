@echo off
setlocal
REM OFFLINE ONLY: graphical Ctrl+V key entry for Windows Credential Manager.
REM No network, no model API call, no bot interaction.
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 -I "%~dp0tools\bcc_kie_credentials_gui.py"
) else (
  where python >nul 2>&1
  if %errorlevel%==0 (
    python -I "%~dp0tools\bcc_kie_credentials_gui.py"
  ) else (
    echo Python 3 not found. No key saved.
  )
)
echo.
echo Claude API remains disabled; no paid calls were made.
pause
