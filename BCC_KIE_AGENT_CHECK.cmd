@echo off
setlocal
REM BCC development agent offline preflight only.
REM Does NOT call Kie.ai, GPT Work, Graphify, GitHub Actions, or trading bots.
set "ROOT=%~dp0"
pushd "%ROOT%" || goto failed
where py >nul 2>&1
if not errorlevel 1 goto use_py
where python >nul 2>&1
if errorlevel 1 goto missing
set "PYTHON=python"
goto run

:use_py
set "PYTHON=py -3"

:run
echo ===== BCC KIE DEVELOPMENT GATEWAY: OFFLINE CHECK =====
%PYTHON% -I "%ROOT%tools\bcc_kie_agent.py" doctor
if errorlevel 1 goto failed
%PYTHON% -I "%ROOT%tools\bcc_kie_agent.py" mock
if errorlevel 1 goto failed
echo.
echo Checking Kie.ai Claude Sonnet 5.5 screenshot protocol (offline only).
%PYTHON% -I "%ROOT%tools\bcc_kie_protocol.py"
if errorlevel 1 goto failed
echo Checking Terra, Kimi, Astra model catalog and blocked transport (offline).
%PYTHON% -I "%ROOT%tools\bcc_kie_models.py"
if errorlevel 1 goto failed
echo Checking screenshot-grounded Terra/Kimi Responses routes, tooling flags and blocked network.
%PYTHON% -I "%ROOT%tools\bcc_kie_responses_protocol.py"
if errorlevel 1 goto failed
echo Running isolated BCC Kie protocol, shared-budget, routing and approval regression tests.
%PYTHON% -B -m unittest discover -s tests -p "test_bcc_kie_*.py" -q
if errorlevel 1 goto failed
echo.
echo OFFLINE CHECKS COMPLETED. No network access or paid calls.
echo Terra/Kimi API routes documented; LIVE compatibility, actual prices and billing remain unverified.
echo Kimi is CHAT-ONLY per Kie.ai; no agent execution.
echo Legacy Claude one-shot authorization remains CONSUMED, not reusable.
echo Graphify/Work integration remains disconnected.
goto finish

:failed
echo OFFLINE CHECK BLOCKED. No paid calls attempted.
goto finish

:missing
echo Python 3 was not found. Nothing was changed.

:finish
popd >nul 2>&1
echo.
pause
endlocal
