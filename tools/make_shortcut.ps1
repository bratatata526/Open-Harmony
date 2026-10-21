# 生成桌面快捷方式（图标 = 像素扳手）
#
# 为什么用脚本生成、而不是把 .lnk 直接入库：
#   .lnk 里存的是【本机绝对路径】（pythonw.exe 位置、仓库位置），
#   提交到 git 只会让队友双击到一个不存在的路径。所以入库的是"生成器"。
#
# 用法（二选一）：
#   1) 在仓库根目录执行：powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1
#   2) 资源管理器里右键本文件 → "使用 PowerShell 运行"
#
# 生成的快捷方式：桌面 → "OpenHarmony 日志分析工具.lnk"

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot          # tools\ 的上一级 = 仓库根
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
$ico = Join-Path $root 'assets\wrench.ico'
$app = Join-Path $root 'tools\launcher.py'

if (-not (Test-Path $pyw)) {
    Write-Host "[X] 找不到虚拟环境：$pyw" -ForegroundColor Red
    Write-Host "    请先运行 start_dashboard.cmd（它会自动检测本机 Python 并重建 .venv）" -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $app)) {
    Write-Host "[X] 找不到启动器：$app" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $ico)) {
    Write-Host "[!] 图标不存在，正在生成…" -ForegroundColor Yellow
    & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'tools\make_icon.py')
}

$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'OpenHarmony 日志分析工具.lnk'

$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = '"' + $app + '"'
$s.WorkingDirectory = $root
$s.IconLocation = $ico + ',0'
$s.Description = 'OpenHarmony 日志分析工具（串口采集 + 智能分析看板）'
$s.Save()

Write-Host "[OK] 已创建桌面快捷方式：" -ForegroundColor Green
Write-Host "     $lnk"
Write-Host "     目标：$pyw" -ForegroundColor DarkGray
Write-Host "     参数：$app" -ForegroundColor DarkGray
Write-Host "     图标：$ico" -ForegroundColor DarkGray
