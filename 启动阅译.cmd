@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "dist\阅译\阅译.exe" (
    start "" "dist\阅译\阅译.exe"
) else if exist ".venv\Scripts\pythonw.exe" (
    set PYTHONDONTWRITEBYTECODE=1
    start "" ".venv\Scripts\pythonw.exe" "main.py"
) else (
    echo Run setup.ps1 first, or download and extract the complete Windows portable release.
    pause
)
