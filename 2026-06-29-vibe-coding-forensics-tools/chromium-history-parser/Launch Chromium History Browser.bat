@echo off
setlocal
cd /d "%~dp0"

set "PY_CMD=py"
%PY_CMD% --version >nul 2>nul
if errorlevel 1 set "PY_CMD=python"

%PY_CMD% -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo PySide6 is not available in the default Python used by this launcher.
    echo Install dependencies with:
    echo %PY_CMD% -m pip install -r requirements.txt
    if errorlevel 1 goto error
)

%PY_CMD% "%~dp0chromium_history_browser.py" %*
if errorlevel 1 goto error
exit /b 0

:error
echo.
echo Chromium History Browser could not start.
pause
exit /b 1
