@echo off
setlocal
cd /d "%~dp0"

set PYTHON_VERSION=3.14
set VENV_DIR=.venv

echo Outlook Evidence Viewer installer
echo Target Python: %PYTHON_VERSION% 64-bit
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python Launcher "py" was not found.
    echo Install 64-bit Python %PYTHON_VERSION%, then run this installer again.
    pause
    exit /b 1
)

py -%PYTHON_VERSION% --version >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python %PYTHON_VERSION% was not found.
    echo Available Python versions:
    py --list
    echo.
    echo Install 64-bit Python %PYTHON_VERSION%, then run this installer again.
    pause
    exit /b 1
)

py -%PYTHON_VERSION% -c "import struct, sys; sys.exit(0 if struct.calcsize('P') * 8 == 64 else 1)" >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python %PYTHON_VERSION% must be 64-bit.
    echo The bundled wheels target win_amd64.
    pause
    exit /b 1
)

if not exist wheels (
    echo ERROR: The wheels folder is missing.
    echo This installer must be run from the extracted distribution folder.
    pause
    exit /b 1
)

echo Creating virtual environment...
py -%PYTHON_VERSION% -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo ERROR: Could not create the virtual environment.
    pause
    exit /b 1
)

echo Installing Outlook Evidence Viewer from local wheels...
"%VENV_DIR%\Scripts\python.exe" -m pip install --no-index --find-links "%~dp0wheels" ost-pst-parser
if errorlevel 1 (
    echo.
    echo ERROR: Installation failed.
    echo Make sure this package contains a libpff wheel for Python %PYTHON_VERSION%,
    echo for example: libpff_python-...-cp314-cp314-win_amd64.whl
    pause
    exit /b 1
)

echo.
echo Installation complete.
echo.
echo You can now:
echo   - double-click Launch Outlook Reader.bat
echo   - drag a PST, OST, PFF, SQLite, DB file onto Launch Outlook Reader.bat
echo   - run "%VENV_DIR%\Scripts\outlook-reader.exe" C:\path\to\mailbox.ost
echo.
pause
