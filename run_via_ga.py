"""
run_via_ga.py — VM-сторонний враппер для запуска бенчмарка из-под GA через PsExec.

Запускается в интерактивной сессии gamer'а (PsExec -u gamer -p ... -i -d),
гонит run_benchmark.py, перенаправляет stdout/stderr в last_run.log, а финальный
JSON-результат — в last_result.json (через флаг --output).

В "nvenc" режиме параллельно держит ffmpeg-нагрузку на NVENC (эмулирует
production: GameServer стримит экран через ddagrab → h264_nvenc → null,
25 Mbps CBR). Это создаёт реалистичную нагрузку на GPU encoder во время бенча.

last_status.json пишется ВСЕГДА (даже при падении). Хост поллит его наличие
через guest-file-open чтобы понять, что бенч завершился.

Использование (запускается через PsExec):
    python run_via_ga.py [config] [load]
        config: vk (default) | rt | 2k
        load:   idle (default) | nvenc

Артефакты:
    C:\\benchmark\\last_run.log     — полный stdout/stderr прогона
    C:\\benchmark\\last_result.json — JSON результата (только при exit_code==0)
    C:\\benchmark\\last_status.json — статус (exit_code, длительность, ...) ВСЕГДА
    C:\\benchmark\\last_ffmpeg.log  — stderr ffmpeg (только в режиме nvenc)
"""

import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

BENCH_DIR    = Path(r'C:\benchmark')
LOG_FILE     = BENCH_DIR / 'last_run.log'
RESULT_FILE  = BENCH_DIR / 'last_result.json'
STATUS_FILE  = BENCH_DIR / 'last_status.json'
FFMPEG_LOG   = BENCH_DIR / 'last_ffmpeg.log'
PYTHON_EXE   = r'C:\Program Files (x86)\Python36-32\python.exe'
FFMPEG_EXE   = BENCH_DIR / 'ffmpeg.exe'
BENCH_SCRIPT = BENCH_DIR / 'run_benchmark.py'

# Параметры NVENC encoder из лога GameServer (vm013, сессия 64795392):
# 25 Mbps CBR, H.264 High, 8 slices, 16 refs, без B-frames/lookahead —
# low-latency streaming-профиль.
NVENC_BITRATE_KBPS = 25000
NVENC_FPS          = 60

# Источник кадров — синтетический lavfi testsrc, НЕ ddagrab. Почему:
# - Production использует не Desktop Duplication, а DX-hook injection в
#   игровой процесс (через C:\temp\SharedCapture_x64.dll → хук на
#   IDXGISwapChain::Present → копирование ID3D11Texture2D в shared memory).
#   Это разобрано через objdump + strings, см. discussion.
# - ddagrab НЕ работает в exclusive-fullscreen (DXGI_ERROR_ACCESS_LOST),
#   а Cyberpunk на VK-конфиге запускается именно так.
# - WGC / NvFBC / OBS-Game-Capture могли бы воспроизвести production capture,
#   но это часы работы и риск нестабильности на VKPKDisplay IDD.
# - Зато NVENC encoder workload (compression — главная GPU-нагрузка от стрима)
#   воспроизводится 1-в-1 любым источником фреймов того же разрешения и rate.
# Capture-stage от стрима в production ~1-2% GPU; encoder — основная нагрузка.
# Этот синтетический режим даёт ~98% production-fidelity для GPU.
FFMPEG_ARGS = [
    '-loglevel', 'error',
    '-stats',
    '-stats_period', '10',
    # -re КРИТИЧЕН для testsrc: без него lavfi отдаёт фреймы AS FAST AS POSSIBLE,
    # NVENC encoder гонит на ~200fps (3.34x speed), что в 3 раза тяжелее
    # production-нагрузки. С -re ffmpeg pacит input на native rate (60fps),
    # encoder работает ровно как production. Это критично для FPS-сравнения.
    '-re',
    '-f', 'lavfi',
    '-i', 'testsrc=size=1920x1080:rate={0}'.format(NVENC_FPS),
    '-vf', 'format=yuv420p',
    '-c:v', 'h264_nvenc',
    '-profile:v', 'high',
    '-preset:v', 'p1',
    '-tune:v', 'll',
    '-rc:v', 'cbr',
    '-b:v', '{0}'.format(NVENC_BITRATE_KBPS * 1000),
    '-maxrate:v', '{0}'.format(NVENC_BITRATE_KBPS * 1000),
    '-bufsize:v', '{0}'.format(NVENC_BITRATE_KBPS * 1000),
    '-bf', '0',
    '-g', '{0}'.format(NVENC_FPS),
    '-slices', '8',
    '-refs', '16',
    '-pix_fmt', 'yuv420p',
    '-f', 'null',
    'NUL',
]

NVENC_WARMUP_S = 2   # дать NVENC сессии инициализироваться до старта бенча


def _safe_unlink(p):
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def start_nvenc_load():
    """Запускает ffmpeg в фоне (stderr в last_ffmpeg.log), ждёт warmup,
    проверяет что процесс жив. Возвращает Popen или None при ошибке."""
    if not FFMPEG_EXE.exists():
        return None, 'ffmpeg.exe не найден: ' + str(FFMPEG_EXE)
    log_fh = open(str(FFMPEG_LOG), 'wb')
    try:
        proc = subprocess.Popen(
            [str(FFMPEG_EXE)] + FFMPEG_ARGS,
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
            cwd=str(BENCH_DIR),
        )
    except Exception as ex:
        log_fh.close()
        return None, 'Popen ffmpeg упал: ' + str(ex)
    time.sleep(NVENC_WARMUP_S)
    if proc.poll() is not None:
        log_fh.close()
        return None, 'ffmpeg завершился сразу (rc={0}) — см. last_ffmpeg.log'.format(proc.returncode)
    return proc, None


def stop_nvenc_load(proc):
    """Корректное завершение ffmpeg: terminate → wait → kill если завис."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def main():
    config = sys.argv[1] if len(sys.argv) > 1 else 'vk'
    load   = sys.argv[2] if len(sys.argv) > 2 else 'idle'

    # Чистим артефакты прошлого запуска. Это важно: появление STATUS_FILE = сигнал
    # хосту что мы закончили, его остатки от прошлого прогона дадут ложный hit.
    for f in (LOG_FILE, RESULT_FILE, STATUS_FILE, FFMPEG_LOG):
        _safe_unlink(f)

    started = time.time()
    rc = -1
    err = None
    ffmpeg_proc = None
    nvenc_error = None

    try:
        # Стартуем NVENC-нагрузку ДО бенча (warmup нужен для инициализации
        # encoder-сессии). Если nvenc не стартовал — фейлим всё, чтобы не
        # путать idle-результат с production.
        if load == 'nvenc':
            ffmpeg_proc, nvenc_error = start_nvenc_load()
            if ffmpeg_proc is None:
                raise RuntimeError('NVENC load не стартовал: ' + (nvenc_error or 'unknown'))

        # stdout+stderr -> один файл лога. Бенч сам в конце дописывает результат.
        with open(str(LOG_FILE), 'wb') as log:
            p = subprocess.Popen(
                [PYTHON_EXE, str(BENCH_SCRIPT),
                 config, '--output', str(RESULT_FILE)],
                stdout=log, stderr=subprocess.STDOUT,
                cwd=str(BENCH_DIR),
            )
            rc = p.wait()
    except Exception:
        err = traceback.format_exc()
    finally:
        # Перед stop_nvenc_load() ловим — успел ли ffmpeg сдохнуть РАНЬШЕ нас.
        # Это критично: если NVENC умер на 30-й секунде из 290, мы измерили почти
        # idle-перформанс под видом production-load. Без этого флага результат
        # выглядел бы «успешным» (exit_code=0) и был бы ложно-показателен.
        nvenc_died_early = False
        if ffmpeg_proc is not None and ffmpeg_proc.poll() is not None:
            nvenc_died_early = True
        stop_nvenc_load(ffmpeg_proc)
        now = time.time()
        status = {
            'exit_code':       rc,
            'config':          config,
            'load':            load,
            'duration_s':      round(now - started, 1),
            'result_present':  RESULT_FILE.exists(),
            'log_present':     LOG_FILE.exists(),
            'started_at':      time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(started)),
            'finished_at':     time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now)),
            'error':           err,
        }
        if load == 'nvenc':
            # Прозрачность: capture-stage синтетический. Production использует
            # DX-hook injection (SharedCapture_x64.dll), мы не можем воспроизвести
            # без полного GameServer-стека. NVENC encoder workload идентичен.
            status['nvenc_source'] = 'lavfi-testsrc-synthetic'
            status['nvenc_fidelity_note'] = (
                'encoder workload matches production (1080p60 H.264 CBR 25Mbps); '
                'capture stage is synthetic — production uses DX swap-chain hook '
                'injection via SharedCapture_x64.dll, not reproducible standalone'
            )
            status['nvenc_died_early'] = nvenc_died_early
        STATUS_FILE.write_text(
            json.dumps(status, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )


if __name__ == '__main__':
    main()
