@echo off
setlocal
REM The only authorized Kie Terra test was attempted on 2026-11-18.
REM This launcher is RETIRED. Only read the existing local ledger/marker.
REM No network, key read, paid calls, QA reruns, ledger writes or bot changes.
set "ROOT=%~dp0"
echo TERRA ONE-SHOT AUTHORIZATION CONSUMED - no further charged requests.
echo Running read-only local diagnostic. No Kie.ai calls.
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -I "%ROOT%tools\bcc_kie_terra_readonly_diagnose.py"
  set "RESULT=%ERRORLEVEL%"
  goto finish
)
where python >nul 2>&1
if errorlevel 1 (
  echo BLOCKED: Python 3 not found. No network requested.
  set "RESULT=2"
  goto finish
)
python -I "%ROOT%tools\bcc_kie_terra_readonly_diagnose.py"
set "RESULT=%ERRORLEVEL%"
:finish
echo.
echo Do NOT delete SQLite HOLD, marker or rerun the paid test.
echo Provider delivery and invoice must be checked in Kie.ai Logs.
pause
endlocal & exit /b %RESULT%
