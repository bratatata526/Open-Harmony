@echo off
rem ===========================================================================
rem  Environment self-check launcher.
rem  ENCODING: saved as GBK/ANSI (cp936) ON PURPOSE -- same reason as
rem  start_dashboard.cmd (cmd.exe + chcp 65001 mis-parses Chinese batch files).
rem ===========================================================================
chcp 936 >nul
cd /d "%~dp0"
title 环境自检
set PYTHONIOENCODING=utf-8

echo ============================================================
echo   环境自检 + 串口 / 板子状态检查
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [X] 找不到 .venv（跨电脑拷贝过来的 .venv 会失效）。
    echo     先运行 start_dashboard.cmd，它会自动检测本机 Python 并重建；
    echo     或手动执行：
    echo       python -m venv .venv
    echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo ---- 1. Python 环境与依赖 ----
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -Check
echo.

echo ---- 2. 串口列表 ----
call :run_py tools\serial_capture.py --list
echo.

echo ---- 3. 校正 DevEco 的烧录端口 ----
echo     板子换 USB 口后 COM 号会变，而 DevEco 里是写死的。
echo     端口号不对时 Upload 会报 "upload port is occupied"（报错有误导性）。
echo.
call :run_py tools\sync_com_port.py
echo.

echo ---- 4. 板子固件一致性校验（约 15 秒）----
echo     想抓开机横幅 / 自检行，请在这 15 秒内按一下板子复位键。
echo.
call :run_py tools\check_firmware.py --port auto --duration 15
echo.

pause
exit /b 0

rem ---- 下面这段用 65001 跑 Python（Python 要输出对勾、箭头之类的符号），
rem      跑完立刻切回 936；本段内全是纯 ASCII，不会有编码错位问题。----
:run_py
chcp 65001 >nul
".venv\Scripts\python.exe" %*
set "RUNPY_RC=%ERRORLEVEL%"
chcp 936 >nul
exit /b %RUNPY_RC%
