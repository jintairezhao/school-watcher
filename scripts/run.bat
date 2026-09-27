@echo off
chcp 65001 >nul
if exist "%~dp0..\.venv\Scripts\python.exe" (
    "%~dp0..\.venv\Scripts\python.exe" "%~dp0launch_desktop.py"
) else (
    python "%~dp0launch_desktop.py"
)
if errorlevel 1 pause
