@echo off
rem ===========================================================================
rem  OpenHarmony dashboard launcher.
rem  ENCODING: this file is saved as GBK/ANSI (cp936) ON PURPOSE.
rem  cmd.exe mis-parses a batch file that contains multi-byte characters while
rem  code page 65001 is active (lines get shifted, "xxx is not recognized").
rem  So: every non-ASCII line below stays under cp936, and 65001 is only used
rem  around the Python call, where all remaining lines are pure ASCII.
rem  Keep this file GBK when editing (VSCode: see .vscode/settings.json).
rem ===========================================================================
chcp 936 >nul
rem ---------------------------------------------------------------------------
rem  【为什么本文件是 GBK 编码】
rem   cmd.exe 在 chcp 65001（UTF-8）下解析含中文的批处理会出现"行错位"，
rem   一大串提示会变成 "xxx 不是内部或外部命令"。所以：
rem     1) 中文提示全部放在 936 代码页下（本机控制台默认就是 936）；
rem     2) 只在调用 Python 前后切到 65001，那段里全是纯 ASCII，不会错位。
rem   编辑本文件请存成 ANSI / GBK。
rem ---------------------------------------------------------------------------
cd /d "%~dp0"
title OpenHarmony 日志分析看板

echo ============================================================
echo   OpenHarmony 日志智能聚合分析工具  -  看板启动器
echo ============================================================
echo.

rem ---- 1. 检查虚拟环境 ------------------------------------------------------
rem  .venv 不能跨电脑拷贝：它内部的 pyvenv.cfg 记着"原电脑 Python 的绝对路径"，
rem  换机器后那个 python.exe 不存在，启动时会报
rem      No Python at 'C:\Users\<原电脑用户名>\...\python.exe'
rem  同时 .venv 里可能还有原电脑编译的 .pyd，本机 Python 版本不同也加载不了。
set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto venv_broken
"%VENV_PY%" -c "import sys" >nul 2>&1
if errorlevel 1 goto venv_broken
"%VENV_PY%" tools\_dep_probe.py >nul 2>&1
if errorlevel 1 goto deps_missing
goto env_ok

:venv_broken
echo [X] 虚拟环境 .venv 不可用（跨电脑拷贝过来的 .venv 一定会失效）。
echo.
set "SYSPY="
for /f "delims=" %%I in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "SYSPY=%%I"
if defined SYSPY if not exist "%SYSPY%" set "SYSPY="
if not defined SYSPY for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "SYSPY=%%I"
if defined SYSPY if not exist "%SYSPY%" set "SYSPY="
if not defined SYSPY goto no_python

echo [i] 已找到本机 Python：
echo       %SYSPY%
echo.
echo     现在用它重建 .venv 并安装依赖（需要联网，约 1 分钟）。
set "ANS="
set /p "ANS=     自动修复？ (Y=修复 / 直接回车=退出) "
if /i not "%ANS%"=="Y" exit /b 1
echo.
echo [*] 删除失效的 .venv ...
if exist ".venv" rmdir /s /q ".venv"
echo [*] 创建 .venv ...
"%SYSPY%" -m venv .venv
if not exist "%VENV_PY%" goto venv_create_failed
echo [*] 安装依赖 ...
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto deps_install_failed
echo.
echo [OK] .venv 已修复完成。
goto env_ok

:no_python
echo [X] 本机没找到可用的 Python（试过 python 和 py -3）。
echo     请先安装 Python 3.8 以上版本，安装时勾选 "Add Python to PATH"，
echo     然后重新运行本脚本。
echo.
pause
exit /b 1

:venv_create_failed
echo [X] 创建 .venv 失败，请手动执行：
echo       "%SYSPY%" -m venv .venv
echo       "%VENV_PY%" -m pip install -r requirements.txt
echo.
pause
exit /b 1

:deps_missing
echo [!] .venv 能启动，但依赖不齐全（需要 serial / openai / yaml / jinja2 / rich）。
echo.
set "ANS="
set /p "ANS=     现在安装依赖？ (Y=安装 / 直接回车=退出) "
if /i not "%ANS%"=="Y" exit /b 1
echo.
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto deps_install_failed
echo.
echo [OK] 依赖安装完成。
goto env_ok

:deps_install_failed
echo [X] 依赖安装失败，请检查网络后手动执行：
echo       "%VENV_PY%" -m pip install -r requirements.txt
echo     国内下载慢可以换镜像：
echo       "%VENV_PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1

:env_ok

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
rem  set PYTHONIOENCODING=utf-8 与 chcp 65001 配套：否则 Python 打印箭头、对勾
rem  这类符号时会因 GBK 编不出来而报错。从 chcp 65001 到 chcp 936 之间全是纯 ASCII。
set PYTHONIOENCODING=utf-8
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" tools/dashboard_server.py --port 8080 --live
chcp 936 >nul

echo.
echo 看板已停止。
pause
