@echo off
cd /d "d:\Jinta\Documents\Claude Code\school-watcher"

set PYTHON=D:\Programs\Python\Python314\python.exe
set CURL=C:\Windows\System32\curl.exe

echo.
echo   ========================================
echo   School Notification Watcher
echo   ========================================
echo.
echo   Starting server...

:: Launch Flask in background (same console)
start "SchoolWatcher" /B "%PYTHON%" app.py

:: Wait for server to be ready
echo   Waiting for server...
:waitloop
timeout /t 2 /nobreak >nul
"%CURL%" -s -o NUL http://localhost:5000/ 2>nul
if %errorlevel% neq 0 goto waitloop

:: Open browser
echo   Opening browser...
start msedge http://localhost:5000/

echo.
echo   Server: http://localhost:5000
echo   Close this window to stop the server.
echo.
pause
