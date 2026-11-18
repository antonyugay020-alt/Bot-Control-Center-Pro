@echo off
setlocal
REM ONE separate, explicitly confirmed Terra test; do not conflate with Claude.
REM This file cannot start payment unless the full isolated Windows offline QA
REM passes AND the local human types APPROVE TERRA 0.01 in Python.
REM No bot process, trading database, model tools or GitHub Actions touched.
set "ROOT=%~dp0"
echo ===== BCC Terra single API connectivity - preflight =====
where powershell.exe >nul 2>&1
if errorlevel 1 goto fail
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools\bcc_kie_offline_windows_qa.ps1"
if errorlevel 1 (
  echo BLOCKED: complete offline suite not green. No paid test sent.
  goto fail
)
echo.
echo OFFLINE PASS. No Kie.ai call has been made yet.
echo ONE real Terra API attempt can now be individually authorized.
echo It is not an agent launch and does not send code or trading data.
echo The local USD 0.01 reserve is NOT a provider-enforced spending cap.
echo Actual tariffs and minimum fees must be checked at Kie.ai.
echo.
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -I "%ROOT%tools\bcc_kie_terra_one_shot.py"
) else (
  where python >nul 2>&1
  if errorlevel 1 goto missing_python
  python -I "%ROOT%tools\bcc_kie_terra_one_shot.py"
)
set "RESULT=%ERRORLEVEL%"
goto finish
:missing_python
echo BLOCKED: Python unavailable. No paid request sent.
goto fail
:fail
set "RESULT=2"
:finish
if "%RESULT%"=="0" (
  echo One Terra API response verified; billing still unverified.
) else (
  echo No confirmed Terra reply. DO NOT RETRY; inspect Kie.ai Logs.
)
echo.
pause
endlocal & exit /b %RESULT%
