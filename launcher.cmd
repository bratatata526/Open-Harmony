@echo off
rem ===========================================================================
rem  日志分析工具 —— 启动器（带控制台，排查用）
rem
rem  正常使用请双击桌面快捷方式（pythonw，无黑窗）。
rem  本脚本保留控制台，用于：启动器起不来时看报错 / 想把日志打在屏幕上。
rem  ENCODING: GBK/ANSI（cp936），与 start_dashboard.cmd 同一约定，别存成 UTF-8。
rem ===========================================================================
chcp 936 >nul
cd /d "%~dp0"
title OpenHarmony 日志分析工具 - 启动器（调试模式）

if not exist ".venv\Scripts\python.exe" (
    echo [X] 找不到 .venv
    echo     请先运行 start_dashboard.cmd，它会自动检测本机 Python 并重建 .venv。
    echo.
    pause
    exit /b 1
)

if not exist "assets\wrench.ico" (
    echo [*] 图标缺失，正在生成 ...
    ".venv\Scripts\python.exe" tools\make_icon.py
)

echo [*] 启动中（关闭窗口即停止服务并释放串口）...
echo.
".venv\Scripts\python.exe" tools\launcher.py --debug
if errorlevel 1 (
    echo.
    echo [X] 启动器异常退出，日志见 logs\launcher.log
    pause
)
