@echo off
REM Double-click this. It just opens the menu in run.py.
cd /d "%~dp0"

python run.py
if errorlevel 9009 (
    echo.
    echo Python isn't on your PATH. Install it from python.org and tick
    echo "Add python.exe to PATH", then double-click this again.
)

echo.
pause
