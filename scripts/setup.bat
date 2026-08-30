@echo off
chcp 65001 >nul
title 学校通知扒取工具 - 安装依赖

cd /d "%~dp0.."

echo.
echo   ========================================
echo   School Notification Watcher - Setup
echo   ========================================
echo.

:: 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Python not found!
    echo   Please install Python 3.10+ from https://www.python.org/downloads/
    echo   Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

python --version
echo.

:: 安装依赖（直接装到全局，省去venv的麻烦）
echo   [1/3] Installing dependencies...
pip install flask flask-sqlalchemy flask-migrate requests beautifulsoup4 lxml apscheduler openai pyyaml python-dotenv curl_cffi scrapling playwright

if %errorlevel% neq 0 (
    echo   [ERROR] Install failed. Check your internet connection.
    pause
    exit /b 1
)

echo   [2/3] Applying database migrations...
flask --app app db upgrade
if %errorlevel% neq 0 (
    echo   [WARNING] Database migration failed. The app may still run if the DB already exists.
)

echo   [3/3] Installing Playwright Chromium browser (to D: drive)...
set PLAYWRIGHT_BROWSERS_PATH=D:\Jinta\Tools\playwright-browsers
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
python -m playwright install chromium

if %errorlevel% neq 0 (
    echo   [WARNING] Playwright browser install failed.
    echo   The app will work but browser fallback for JS pages is disabled.
    echo   You can install later with: python -m playwright install chromium
)

echo.
echo   ========================================
echo   Setup complete!
echo.
echo   Double-click run.bat to start.
echo   Then open http://localhost:5000
echo   ========================================
echo.
pause
