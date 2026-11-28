@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Start a separate persistent PAPER profile alongside the existing BCC.
set "BCC_DATA_ROOT=%~dp0..\BCC_V39_PAPER_DATA"
call "%~dp0START.bat"
exit /b %errorlevel%
