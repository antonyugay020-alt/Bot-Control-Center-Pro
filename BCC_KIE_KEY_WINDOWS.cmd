@echo off
setlocal
REM OFFLINE ONLY. No Kie.ai request, no bot or Windows service control.
set "REPO=%~dp0"
where py >nul 2>&1
if not errorlevel 1 goto py_launcher
where python >nul 2>&1
if errorlevel 1 goto no_python
set "PY_CMD=python"
goto launch

:py_launcher
set "PY_CMD=py -3"

:launch
echo Opening the BCC key-entry window. Use Ctrl+V or Paste.
echo Do not paste the API key into this console window.
%PY_CMD% -I "%REPO%tools\bcc_kie_credentials_gui.py"
set "GUI_EXIT=%errorlevel%"
echo.
if "%GUI_EXIT%"=="0" (
  echo GUI confirmed: key saved.
) else (
  echo GUI did NOT confirm saving the key. Exit code: %GUI_EXIT%
)
echo Checking presence only (key value will remain hidden):
%PY_CMD% -I "%REPO%tools\bcc_kie_credentials.py" status
if errorlevel 1 echo Status check failed. Key presence unknown.
echo No paid calls were made. Claude API remains disabled.
goto finish

:no_python
echo Python 3 was not found. GUI not started. No key saved.

:finish
echo.
pause
endlocal
