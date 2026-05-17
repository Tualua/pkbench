"""
run_via_ga.py — VM-сторонний враппер для запуска бенчмарка из-под GA через PsExec.

Запускается в интерактивной сессии gamer'а (PsExec -u gamer -p ... -i -d),
гонит run_benchmark.py, перенаправляет stdout/stderr в last_run.log, а финальный
JSON-результат — в last_result.json (через флаг --output).

last_status.json пишется ВСЕГДА (даже при падении). Хост поллит его наличие
через guest-file-open чтобы понять, что бенч завершился.

Использование (запускается через PsExec):
    python run_via_ga.py [config]
        config: vk (default) | rt | 2k

Артефакты:
    C:\\benchmark\\last_run.log     — полный stdout/stderr прогона
    C:\\benchmark\\last_result.json — JSON результата (только при exit_code==0)
    C:\\benchmark\\last_status.json — статус (exit_code, длительность, ...) ВСЕГДА
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
PYTHON_EXE   = r'C:\Program Files (x86)\Python36-32\python.exe'
BENCH_SCRIPT = BENCH_DIR / 'run_benchmark.py'


def _safe_unlink(p):
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def main():
    config = sys.argv[1] if len(sys.argv) > 1 else 'vk'

    # Чистим артефакты прошлого запуска. Это важно: появление STATUS_FILE = сигнал
    # хосту что мы закончили, его остатки от прошлого прогона дадут ложный hit.
    for f in (LOG_FILE, RESULT_FILE, STATUS_FILE):
        _safe_unlink(f)

    started = time.time()
    rc = -1
    err = None
    try:
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
        now = time.time()
        status = {
            'exit_code':      rc,
            'config':         config,
            'duration_s':     round(now - started, 1),
            'result_present': RESULT_FILE.exists(),
            'log_present':    LOG_FILE.exists(),
            'started_at':     time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(started)),
            'finished_at':    time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now)),
            'error':          err,
        }
        STATUS_FILE.write_text(
            json.dumps(status, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )


if __name__ == '__main__':
    main()
