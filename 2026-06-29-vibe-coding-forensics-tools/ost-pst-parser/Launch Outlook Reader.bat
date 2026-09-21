@echo off
setlocal
cd /d "%~dp0"
set OUTLOOK_READER_BATCH_LAUNCHER=1

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -m ost_pst_parser %*
    goto :after_run
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m ost_pst_parser %*
) else (
    python -m ost_pst_parser %*
)

:after_run
if errorlevel 1 (
    echo.
    echo Outlook Reader exited with an error.
    pause
)
