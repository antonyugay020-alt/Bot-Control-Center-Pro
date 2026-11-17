@echo off
setlocal
REM OFFLINE ONLY: this launcher never contacts Kie.ai or starts BCC bots.
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 -I "%~dp0tools\bcc_kie_credentials.py" setup
) else (
  where python >nul 2>&1
  if %errorlevel%==0 (
    python -I "%~dp0tools\bcc_kie_credentials.py" setup
  ) else (
    echo Python 3 was not found. No key saved.
  )
)
echo.
echo This does NOT activate Claude or authorize any paid calls.
pause
