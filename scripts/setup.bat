@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo Installing School Watcher in a project virtual environment...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-browser.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 echo Chromium download failed. Static sources remain available; see the browser setup guide.
".venv\Scripts\python.exe" scripts\migrate_safely.py
if errorlevel 1 goto fail
echo Setup complete. Run scripts\run.bat to start.
echo AI is optional. Browser setup details are in deploy\README.md.
pause
exit /b 0
:fail
echo Setup failed. Please review the error above.
pause
exit /b 1
