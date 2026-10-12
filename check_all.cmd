@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 环境自检

echo ============================================================
echo   环境自检 + 串口 / 板子状态检查
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [X] 找不到 .venv，请先执行： python -m venv .venv
    pause
    exit /b 1
)

echo ---- 1. Python 环境与依赖 ----
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -Check
echo.

echo ---- 2. 串口列表 ----
".venv\Scripts\python.exe" tools\serial_capture.py --list
echo.

echo ---- 3. 校正 DevEco 的烧录端口 ----
echo     板子换 USB 口后 COM 号会变，而 DevEco 里是写死的。
echo     端口号不对时 Upload 会报 "upload port is occupied"（报错有误导性）。
echo.
".venv\Scripts\python.exe" tools\sync_com_port.py
echo.

echo ---- 4. 板子固件一致性校验（约 15 秒）----
echo     想抓开机横幅 / 自检行，请在这 15 秒内按一下板子复位键。
echo.
".venv\Scripts\python.exe" tools\check_firmware.py --port auto --duration 15
echo.

pause