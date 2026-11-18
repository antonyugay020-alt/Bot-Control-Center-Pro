@echo off
setlocal
REM The one authorized paid Kie.ai request was consumed on 2026-11-17.
REM This historical launcher is now READ-ONLY; it NEVER sends API requests.
set "ROOT=%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -I "%ROOT%tools\bcc_kie_ledger_review.py"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python 3 not found. No action taken.
  ) else (
    python -I "%ROOT%tools\bcc_kie_ledger_review.py"
  )
)
echo.
echo ONE-SHOT AUTHORIZATION CONSUMED. No paid calls are permitted.
echo The logged Kie.ai HTTP 502 used zero credits per the provider UI.
echo Provider billing remains to be reconciled. Do not erase the budget ledger.
echo.
pause
endlocal
