@echo off
setlocal
REM BCC development agent offline preflight only.
REM Does NOT call Kie.ai, GPT Work, Graphify, GitHub Actions, or trading bots.
set "ROOT=%~dp0"
where py >nul 2>&1
if not errorlevel 1 goto use_py
where python >nul 2>&1
if errorlevel 1 goto missing
set "PYTHON=python"
goto run

:use_py
set "PYTHON=py -3"

:run
echo ===== BCC CLAUDE AGENT: OFFLINE CHECK =====
%PYTHON% -I "%ROOT%tools\bcc_kie_agent.py" doctor
if errorlevel 1 goto failed
%PYTHON% -I "%ROOT%tools\bcc_kie_agent.py" mock
if errorlevel 1 goto failed
echo.
echo OFFLINE CHECKS COMPLETED. No network access or paid calls.
echo Graphify and Kie.ai API compatibility remain unverified.
goto finish

:failed
echo OFFLINE CHECK BLOCKED. No paid calls attempted.
goto finish

:missing
echo Python 3 was not found. Nothing was changed.

:finish
echo.
pause
endlocal
