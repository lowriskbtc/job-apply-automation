@echo off
REM Double-click this for the window. (Start.bat is the terminal menu.)
cd /d "%~dp0"

start "" pythonw gui.py
if errorlevel 9009 (
    echo.
    echo Python isn't on your PATH. Install it from python.org and tick
    echo "Add python.exe to PATH", then double-click this again.
    pause
)
