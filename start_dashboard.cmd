@echo off
chcp 65001 >nul
cd /d "%~dp0"
title OpenHarmony 日志分析看板

echo ============================================================
echo   OpenHarmony 日志智能聚合分析工具  -  看板启动器
echo ============================================================
echo.

rem ---- 1. 检查虚拟环境 ----
if not exist ".venv\Scripts\python.exe" (
    echo [X] 找不到 .venv，请先在仓库根目录执行：
    echo       python -m venv .venv
    echo       .venv\Scripts\python.exe -m pip install pyserial
    echo       .venv\Scripts\python.exe -m pip install "openai^<1.60"
    echo.
    pause
    exit /b 1
)

rem ---- 2. 检查 8080 是否被占（必须匹配 ":8080 " 带空格，否则误匹配 :80800）----
netstat -ano | findstr "LISTENING" | findstr ":8080 " >nul 2>&1
if not errorlevel 1 (
    echo [!] 8080 端口已被占用，很可能是上一次的看板还在跑。
    echo     请先关掉那个窗口（在窗口里按 Ctrl+C），再运行本脚本。
    echo.
    echo     查是谁占的：
    echo       netstat -ano ^| findstr :8080
    echo.
    pause
    exit /b 1
)

rem ---- 3. 使用须知 ----
echo [重要] 看板运行期间会独占串口。
echo        DevEco 此时无法 Upload / 烧录 / 开 Monitor。
echo        要烧录请先关掉本窗口。
echo.
echo [提示] 刚烧录完如果看不到数据，请物理拔插一次 USB。
echo        HiBurn 会把芯片留在 bootloader，按复位键不够。
echo.
echo 浏览器请打开： http://127.0.0.1:8080
echo 停止看板：在本窗口按 Ctrl+C
echo ============================================================
echo.

rem ---- 4. 启动 ----
rem set PYTHONIOENCODING=utf-8：与上面的 chcp 65001 配套，
rem   否则 Python 打印 "▶" 这类字符时会因 GBK 编不出来而崩溃。
rem -ExecutionPolicy Bypass 只对本次调用生效，不改系统设置。
set PYTHONIOENCODING=utf-8
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" tools/dashboard_server.py --port 8080 --live

echo.
echo 看板已停止。
pause