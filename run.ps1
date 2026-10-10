# run.ps1 - unified entry: load .env then run python with venv
#
# Usage:
#   .\run.ps1 tools/analyze_demo.py --input eval/dataset/d2_v2.log
#   .\run.ps1 -Check
#
# If "script execution disabled", run once:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Args,
    [switch] $Check
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

# ---- 1. locate venv python ----
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host "[X] venv not found: $venvPy" -ForegroundColor Red
    Write-Host "    create it first:" -ForegroundColor Yellow
    Write-Host "      python -m venv .venv" -ForegroundColor Yellow
    exit 1
}

# ---- 2. load .env ----
$envFile = Join-Path $root '.env'
$keyLoaded = $false
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim().Trim('"').Trim("'")
        if ($k -eq 'DEEPSEEK_API_KEY') {
            if ($v -and ($v -notmatch 'REPLACE_ME') -and ($v -notmatch '在这里')) {
                $env:DEEPSEEK_API_KEY = $v
                $keyLoaded = $true
            }
        } elseif ($v) {
            Set-Item -Path "env:$k" -Value $v
        }
    }
}

# ---- 3. check mode ----
if ($Check -or $Args.Count -eq 0) {
    Write-Host "======== ENV CHECK ========"
    $pyinfo = & $venvPy -c "import sys;print(sys.version.split()[0],'|',sys.executable)"
    Write-Host ("Python            {0}" -f $pyinfo)
    $pkgs = & $venvPy "tools\_dep_probe.py"
    Write-Host ("Dependencies      {0}" -f $pkgs)
    if ($keyLoaded) {
        $n = [Math]::Min(7, $env:DEEPSEEK_API_KEY.Length)
        Write-Host ("DEEPSEEK_API_KEY  loaded ({0}...)  OK" -f $env:DEEPSEEK_API_KEY.Substring(0, $n)) -ForegroundColor Green
    } else {
        Write-Host "DEEPSEEK_API_KEY  NOT SET" -ForegroundColor Yellow
        Write-Host "  -> edit .env, replace the placeholder with your real key" -ForegroundColor Yellow
        Write-Host "  -> apply at: https://platform.deepseek.com/api_keys" -ForegroundColor Yellow
        Write-Host "  (pipeline still works: LLM auto-degrades to rule engine)" -ForegroundColor DarkGray
    }
    if ($Args.Count -eq 0) {
        Write-Host ""
        Write-Host "Usage: .\run.ps1 <script> [args...]" -ForegroundColor Cyan
        Write-Host "E.g.:  .\run.ps1 tools/analyze_demo.py --input eval/dataset/d2_v2.log" -ForegroundColor Cyan
    }
    exit 0
}

# ---- 4. run target ----
$script = $Args[0]
if ($Args.Count -gt 1) { $rest = $Args[1..($Args.Count - 1)] } else { $rest = @() }

if (-not (Test-Path $script)) {
    Write-Host "[X] script not found: $script" -ForegroundColor Red
    exit 1
}

Write-Host "> $script $($rest -join ' ')" -ForegroundColor DarkGray
if ($keyLoaded) {
    Write-Host "  LLM: enabled" -ForegroundColor DarkGray
} else {
    Write-Host "  LLM: degraded mode (no API key)" -ForegroundColor DarkGray
}
Write-Host ""

& $venvPy $script @rest
exit $LASTEXITCODE