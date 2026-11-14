@echo off
REM Double-click to start Civic Air Quality Briefing Studio on Windows (needs Python 3.11+ only).
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 run.py %*
) else (
    where python >nul 2>nul
    if %ERRORLEVEL%==0 (
        python run.py %*
    ) else (
        echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" in the installer, then run this file again.
        pause
        exit /b 1
    )
)
if %ERRORLEVEL% NEQ 0 pause
endlocal
