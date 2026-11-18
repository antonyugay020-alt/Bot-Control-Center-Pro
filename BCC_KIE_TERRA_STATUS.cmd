@echo off
setlocal
REM READ-ONLY: inspect local Terra one-shot marker and SQLite rows.
REM No provider calls, credentials, key reads, billing updates or bot access.
set "ROOT=%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -I "%ROOT%tools\bcc_kie_terra_readonly_diagnose.py"
  set "RESULT=%ERRORLEVEL%"
  goto done
)
where python >nul 2>&1
if errorlevel 1 (
  echo BLOCKED: Python unavailable; no network sent.
  set "RESULT=2"
  goto done
)
python -I "%ROOT%tools\bcc_kie_terra_readonly_diagnose.py"
set "RESULT=%ERRORLEVEL%"
:done
endlocal & exit /b %RESULT%
