@echo off
:: simulate_nvenc_load.bat
:: Запускает ffmpeg с NVENC захватом экрана, максимально близко к параметрам GameServer.
::
:: Параметры из лога GameServer (vm013, сессия 64795392):
::   1920x1080, H.264 High profile, 8 slices, 16 ref frames
::   Bitrate: adaptive 1-25 Mbps, target 25 Mbps (peak во время игры)
::   Capture: Desktop Duplication API (ddagrab)
::   Constrained encoding: включён
::   fullRGB -> YUV420p конвертация
::   No lookahead, no psycho-visual, no B-frames (low-latency стриминг)
::
:: Вывод идёт в null — диск не нагружается, нагружается только NVENC.
::
:: Использование:
::   simulate_nvenc_load.bat          -- запустить с параметрами из лога
::   simulate_nvenc_load.bat 15000    -- задать конкретный битрейт в kbps
::
:: Требования:
::   ffmpeg с поддержкой ddagrab и h264_nvenc (ffmpeg.org builds)
::   NVIDIA GPU с NVENC

setlocal

:: Битрейт: берём peak из лога (25 Mbps target)
:: Можно переопределить первым аргументом
set BITRATE_KBPS=%1
if "%BITRATE_KBPS%"=="" set BITRATE_KBPS=25000

set BITRATE_BPS=%BITRATE_KBPS%000

:: Параметры захвата
set FPS=60
set WIDTH=1920
set HEIGHT=1080

echo.
echo === NVENC Load Simulator ===
echo Параметры: %WIDTH%x%HEIGHT%@%FPS%fps, %BITRATE_KBPS% kbps
echo Соответствует: GameServer vm013, сессия 64795392
echo Вывод: null (диск не используется)
echo Остановить: Ctrl+C
echo.

:: ddagrab: захват через Desktop Duplication API (как у GameServer)
:: h264_nvenc с параметрами максимально близкими к логу:
::   -profile:v high        = profile 100 (High)
::   -slices 8              = sliceCount: 8
::   -refs 16               = maxNumRefFrames: 16
::   -cbr 1 -b:v ...        = constrained encoding + fixed bitrate
::   -preset p1             = низкая латентность (стриминг), без lookahead
::   -tune ll               = low-latency (аналог streaming mode)
::   -rc cbr                = постоянный битрейт (constrained_encoding)
::   -bf 0                  = без B-frames (low-latency стриминг)
::   -g 60                  = keyframe каждую секунду (типично для стриминга)
::   -pix_fmt yuv420p       = fullRGB -> YUV420 как у GameServer

ffmpeg ^
  -f lavfi ^
  -i "ddagrab=output_idx=0:framerate=%FPS%:draw_mouse=0" ^
  -vf "hwdownload,format=bgr0,format=yuv420p" ^
  -c:v h264_nvenc ^
  -profile:v high ^
  -preset:v p1 ^
  -tune:v ll ^
  -rc:v cbr ^
  -b:v %BITRATE_BPS% ^
  -maxrate:v %BITRATE_BPS% ^
  -bufsize:v %BITRATE_BPS% ^
  -bf 0 ^
  -g %FPS% ^
  -slices 8 ^
  -refs 16 ^
  -pix_fmt yuv420p ^
  -f null ^
  NUL

endlocal
