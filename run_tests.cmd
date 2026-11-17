@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=py"
)

echo Running syntax check...
%PYTHON_EXE% -m compileall -q freelance_bot
if errorlevel 1 goto failed

echo Running tests...
%PYTHON_EXE% -m pytest -q -p no:cacheprovider
if errorlevel 1 goto failed

echo.
echo All checks passed.
pause
exit /b 0

:failed
echo.
echo Checks failed. See the message above.
pause
exit /b 1
