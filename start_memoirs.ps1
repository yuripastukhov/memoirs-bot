# Запуск бота «Мемуары» (bot25.py) на этом сервере.
#
#   .\start_memoirs.ps1            # запустить
#   .\start_memoirs.ps1 -Stop      # остановить
#
# Отличия от исходного start_bot.bat: свой venv, фиксированный PATH с ffmpeg
# (нужен pydub для голосовых), логи в data\logs, pid-файл для сторожа.

param(
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

$app = $PSScriptRoot
$python = Join-Path $app '.venv\Scripts\python.exe'
$script = Join-Path $app 'bot25.py'
$logDir = Join-Path $app 'logs'
$pidFile = Join-Path $logDir 'memoirs.pids.json'
$ffmpegRoot = Join-Path (Split-Path $app -Parent) 'bin\ffmpeg\ffmpeg-master-latest-win64-gpl\bin'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Get-BotProcess {
    if (-not (Test-Path $pidFile)) { return $null }
    try {
        $data = Get-Content $pidFile -Raw -Encoding utf8 | ConvertFrom-Json
        return Get-Process -Id $data.bot -ErrorAction SilentlyContinue
    } catch {
        return $null
    }
}

if ($Stop) {
    $process = Get-BotProcess
    if ($process) {
        Stop-Process -Id $process.Id -Force
        Write-Host "бот остановлен (pid $($process.Id))"
    } else {
        Write-Host "бот не запущен"
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    return
}

if (Get-BotProcess) {
    Write-Host "бот уже запущен"
    return
}

if (-not (Test-Path $python)) { throw "нет venv: $python" }
if (-not (Test-Path $script)) { throw "нет скрипта: $script" }

# pydub и speech_recognition ищут ffmpeg в PATH
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
if (Test-Path $ffmpegRoot) {
    $env:PATH = "$ffmpegRoot;$env:PATH"
}

$stdout = Join-Path $logDir 'memoirs.log'
$stderr = Join-Path $logDir 'memoirs.err.log'

$process = Start-Process -FilePath $python -ArgumentList 'bot25.py' `
    -WorkingDirectory $app -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr

@{ bot = $process.Id; started = (Get-Date).ToString('o') } |
    ConvertTo-Json | Set-Content -Path $pidFile -Encoding utf8

Write-Host "бот запущен (pid $($process.Id))"
Write-Host "лог: $stderr"
