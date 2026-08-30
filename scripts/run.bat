@echo off
chcp 65001 >nul
title 学校通知扒取工具

cd /d "%~dp0.."

echo.
echo   ========================================
echo   School Notification Watcher v1.0
echo   ========================================
echo.
echo   Starting server...
echo.

:: 启动 Flask（新窗口隐藏运行）
if exist "venv\Scripts\python.exe" (
    start "" /B venv\Scripts\python.exe app.py
) else (
    start "" /B python app.py
)

:: 等待服务器就绪
echo   Waiting for server to be ready...
:waitloop
timeout /t 1 /nobreak >nul
curl -s -o NUL http://localhost:5000/ 2>nul
if %errorlevel% neq 0 goto waitloop

:: 自动打开浏览器
echo   Opening browser...
start msedge http://localhost:5000/

echo.
echo   ========================================
echo   Server running at http://localhost:5000
echo   Close this window to stop the server.
echo   ========================================
echo.

pause
