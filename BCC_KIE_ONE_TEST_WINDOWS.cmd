@echo off
setlocal
REM USER-AUTHORIZED ONE-TIME TEST ONLY. Can send at most one charged request
REM after a successful offline regression and an explicit typed confirmation.
REM No live BCC bots, database or Windows service are involved.
set "ROOT=%~dp0"
pushd "%ROOT%" || goto failed
where py >nul 2>&1
if not errorlevel 1 goto py_launcher
where python >nul 2>&1
if errorlevel 1 goto missing_python
set "PYTHON=python"
goto tests

:py_launcher
set "PYTHON=py -3"

:tests
echo ===== BCC KIE ONE-TIME TEST - PRE-FLIGHT =====
%PYTHON% -B -m unittest discover -s tests -p "test_bcc_kie_*.py" -q
if errorlevel 1 goto regression_failed
echo.
echo Offline tests passed. A real Kie.ai request has NOT yet been sent.
echo ONE request is authorized, with a USD 0.01 local reservation.
echo Actual provider billing must be checked separately on Kie.ai.
echo If you do not want the request, just close the window or type anything
echo other than APPROVE 0.01 when prompted.
echo.
%PYTHON% -I "%ROOT%tools\bcc_kie_one_shot.py"
set "RESULT=%errorlevel%"
echo.
if "%RESULT%"=="0" (
  echo One Kie.ai Claude reply confirmed. No additional requests authorized.
) else (
  echo No confirmed reply. DO NOT retry; inspect the provider bill.
)
goto finish

:regression_failed
echo Offline regression FAILED. No paid Kie.ai request has been sent.
goto failed

:missing_python
echo Python 3 was not found. No provider request sent.
goto failed

:failed
set "RESULT=2"

:finish
popd >nul 2>&1
echo.
pause
endlocal & exit /b %RESULT%
