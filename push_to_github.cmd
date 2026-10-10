@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 推送到 GitHub

echo ============================================================
echo   推送到 GitHub  -  OpenHarmony 日志智能聚合分析工具
echo ============================================================
echo.

echo [待推送的提交]
git log --oneline -1
echo.
echo [远程仓库]
git remote get-url origin
echo.

echo ------------------------------------------------------------
echo   第 1 步：推送前安全检查（确认没有密钥）
echo ------------------------------------------------------------
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" tools\secret_scan.py --tracked
) else (
    python tools\secret_scan.py --tracked
)
if errorlevel 1 (
    echo.
    echo [X] 扫描发现疑似密钥，已【中止推送】！
    echo     请先处理掉密钥再重跑本脚本。
    echo.
    pause
    exit /b 1
)
echo.

echo ------------------------------------------------------------
echo   第 2 步：推送到 GitHub
echo ------------------------------------------------------------
echo [提示] 稍后可能弹出浏览器要求授权 GitHub，请点确认。
echo        如果没弹窗或授权失败，见下面"推送失败"的排查提示。
echo.
echo 按任意键开始推送...
pause >nul
echo.

git push -u origin main
if errorlevel 1 (
    echo.
    echo ============================================================
    echo   [X] 推送失败
    echo ============================================================
    echo   常见原因：
    echo     1. 未授权：浏览器窗口没点确认，或被安全软件拦截
    echo        解决：重跑本脚本，注意看有没有弹窗
    echo     2. 仓库非空：在 GitHub 建仓库时勾了 README / .gitignore / license
    echo        解决：删掉远程仓库重建一个【完全空的】，或在 GitHub 上
    echo              删除那个初始提交后再推
    echo     3. 网络问题：国内访问 GitHub 可能不稳定，可多重试几次
    echo.
    echo   手动排查：
    echo       git ls-remote origin
    echo       git push -u origin main
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ✔ 推送成功
echo ============================================================
echo.
echo   仓库地址： https://github.com/moudou517/openharmony-log-analyzer
echo.
echo   队友克隆命令：
echo       git clone https://github.com/moudou517/openharmony-log-analyzer.git
echo.
pause