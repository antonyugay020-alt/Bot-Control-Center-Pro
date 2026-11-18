@echo off
setlocal
REM Complete BCC Kie.ai offline regression; NO paid API calls, no live bots.
REM Only the separate BCC_AGENT_GATEWAY checkout is permitted by PS1.
set "ROOT=%~dp0"
where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo BLOCKED: Windows PowerShell is missing. No tests run.
  set "RESULT=2"
  goto finish
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools\bcc_kie_offline_windows_qa.ps1"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo BCC KIE OFFLINE QA: BLOCKED OR FAILED - DO NOT ASSUME PASS.
) else (
  echo BCC KIE OFFLINE QA: PASS. No provider calls, not Windows Qt E2E.
)
:finish
echo.
pause
endlocal & exit /b %RESULT%
