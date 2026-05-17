# run_benchmark_with_nvenc.ps1
# Запускает симулятор NVENC нагрузки и бенчмарк параллельно.
# Останавливает ffmpeg автоматически когда бенчмарк завершается.
#
# Использование:
#   powershell -ExecutionPolicy Bypass -File run_benchmark_with_nvenc.ps1
#   powershell -ExecutionPolicy Bypass -File run_benchmark_with_nvenc.ps1 -VmName vm013.hv05.pk.minecolo.io
#   powershell -ExecutionPolicy Bypass -File run_benchmark_with_nvenc.ps1 -BitrateKbps 15000

param(
    [string]$VmName     = "vm013.test.local",
    [int]$BitrateKbps   = 25000,
    [int]$Fps           = 60,
    [string]$FfmpegPath = "ffmpeg"  # если не в PATH, укажи полный путь
)

$BenchmarkScript = Join-Path $PSScriptRoot "run_benchmark.py"
$PythonExe = "C:\Program Files (x86)\Python36-32\python.exe"

if (-not (Test-Path $BenchmarkScript)) {
    Write-Error "Не найден: $BenchmarkScript"
    exit 1
}

# Проверяем ffmpeg
try {
    & $FfmpegPath -version 2>&1 | Out-Null
} catch {
    Write-Error "ffmpeg не найден. Укажи путь через -FfmpegPath"
    exit 1
}

$BitrateBps = $BitrateKbps * 1000

Write-Host ""
Write-Host "=== Benchmark + NVENC Load Simulator ===" -ForegroundColor Cyan
Write-Host "VM name  : $VmName"
Write-Host "Bitrate  : $BitrateKbps kbps"
Write-Host "FPS      : $Fps"
Write-Host ""

# Запускаем ffmpeg NVENC захват в фоне
Write-Host "[1/2] Запуск NVENC симулятора..." -ForegroundColor Yellow
$ffmpegArgs = @(
    "-f", "lavfi",
    "-i", "ddagrab=output_idx=0:framerate=$($Fps):draw_mouse=0",
    "-vf", "hwdownload,format=bgr0,format=yuv420p",
    "-c:v", "h264_nvenc",
    "-profile:v", "high",
    "-preset:v", "p1",
    "-tune:v", "ll",
    "-rc:v", "cbr",
    "-b:v", "$BitrateBps",
    "-maxrate:v", "$BitrateBps",
    "-bufsize:v", "$BitrateBps",
    "-bf", "0",
    "-g", "$Fps",
    "-slices", "8",
    "-refs", "16",
    "-pix_fmt", "yuv420p",
    "-f", "null",
    "NUL"
)

$ffmpegProc = Start-Process `
    -FilePath $FfmpegPath `
    -ArgumentList $ffmpegArgs `
    -PassThru `
    -WindowStyle Minimized

Write-Host "  ffmpeg PID: $($ffmpegProc.Id)" -ForegroundColor Gray

# Небольшая пауза чтобы NVENC успел инициализироваться
Start-Sleep -Seconds 2

# Проверяем что ffmpeg запустился
if ($ffmpegProc.HasExited) {
    Write-Error "ffmpeg завершился сразу. Проверь что ddagrab и h264_nvenc доступны."
    Write-Host "Попробуй: ffmpeg -f lavfi -i ddagrab=output_idx=0:framerate=60 -vf hwdownload,format=bgr0,format=yuv420p -c:v h264_nvenc -t 5 -f null NUL"
    exit 1
}

Write-Host "  NVENC активен" -ForegroundColor Green
Write-Host ""

# Запускаем бенчмарк
Write-Host "[2/2] Запуск бенчмарка..." -ForegroundColor Yellow
$env:BENCHMARK_STANDALONE = "1"
$benchArgs = @(
    $BenchmarkScript,
    "--vm", $VmName
)

$benchProc = Start-Process `
    -FilePath $PythonExe `
    -ArgumentList $benchArgs `
    -PassThru `
    -Wait  # ждём завершения бенчмарка

Write-Host ""
Write-Host "Бенчмарк завершён (exit code: $($benchProc.ExitCode))" -ForegroundColor Cyan

# Останавливаем ffmpeg
Write-Host "Останавливаем NVENC симулятор..." -ForegroundColor Yellow
if (-not $ffmpegProc.HasExited) {
    $ffmpegProc.Kill()
    $ffmpegProc.WaitForExit(3000) | Out-Null
    Write-Host "  ffmpeg остановлен" -ForegroundColor Green
} else {
    Write-Host "  ffmpeg уже завершился" -ForegroundColor Gray
}

Write-Host ""
Write-Host "=== Готово ===" -ForegroundColor Cyan
