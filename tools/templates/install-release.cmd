@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0安装阅译.ps1"
if errorlevel 1 (
    echo Installation failed. Check the error above. Existing library folders were not overwritten.
)
pause
