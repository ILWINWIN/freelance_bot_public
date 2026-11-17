@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m freelance_bot
) else (
    py -m freelance_bot
)

if errorlevel 1 (
    echo.
    echo Application finished with an error. See the message above.
    pause
)
endlocal
