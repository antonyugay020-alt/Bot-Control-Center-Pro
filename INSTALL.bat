@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"
if errorlevel 1 goto failed
rem Install only into this package's venv. Never touch trading state.
set "VENV=%~dp0.venv"
if exist "%VENV%\Scripts\python.exe" goto install_packages
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv "%VENV%"
) else (
    python -m venv "%VENV%"
)
if errorlevel 1 goto failed
:install_packages
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 goto failed
"%VENV%\Scripts\python.exe" -c "import PySide6, requests, psutil"
if errorlevel 1 goto failed
if not exist "%VENV%\Scripts\pythonw.exe" goto failed
echo [BCC] Private Python/Qt runtime ready.
if /I not "%BCC_INSTALL_NO_PAUSE%"=="1" pause
exit /b 0
:failed
echo [BCC ERROR] Python/Qt installation failed. Check pip output, Python and network.
echo [BCC ERROR] Existing MY_BOTS and PAPER accounts were not changed.
if /I not "%BCC_INSTALL_NO_PAUSE%"=="1" pause
exit /b 1
