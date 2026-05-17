"""
run_benchmark.py — автономный запуск Cyberpunk-бенчмарка без GameServer и
без Benchmark.Gta.exe.

Шаги:
  1. Прогон init_script_reconstructed.py с заглушкой вместо start_benchmark.
     Нужен ради подготовки окружения (шейдеры через GameShaders.setup_dx_shaders,
     ярлык Cyberpunk2077.lnk.lnk, манифесты, SystemInfo.json).
  2. Запуск cyberpunk_runner.run_cyberpunk_benchmark() — сам качает
     UserSettings.json с CDN, запускает игру, ждёт результат, парсит.

Использование:
    python run_benchmark.py [config] [--output FILE]
        config: vk (default, 120fps test) | rt (RayTracing) | 2k
        --output FILE: куда дополнительно записать JSON результата
                        (всё равно печатается в stdout)

Steam credentials больше не нужны — Cyberpunk запускается напрямую через
Cyberpunk2077.exe, а Steam креды требовались только в Mark/Wukong/Steam-стека
оригинального Benchmark.Gta.exe.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
_parser.add_argument('config', nargs='?', default='vk', choices=['vk', 'rt', '2k'])
_parser.add_argument('--output', type=Path, default=None,
                     help='Куда дополнительно записать JSON результата')
_args = _parser.parse_args()

CYBERPUNK_CONFIG = _args.config
OUTPUT_FILE      = _args.output

WIDTH    = 1920
HEIGHT   = 1080
FPS      = 60
DNS_NAME = 'standalone.benchmark.i'  # суффикс .i всё ещё нужен init_script'у для логики ярлыка

INIT_SCRIPT = Path(__file__).parent / 'init_script_reconstructed.py'

# -- Шаг 1: init_script с заглушкой вместо start_benchmark -------------------
print('[wrapper] Шаг 1: подготовка окружения через init_script...')

if not INIT_SCRIPT.exists():
    sys.exit('ERROR: init_script_reconstructed.py not found')

# Патчим start_benchmark() чтобы Benchmark.Gta.exe не запустился из init_script
src = INIT_SCRIPT.read_text(encoding='utf-8')
old_func = (
    'def start_benchmark():\n'
    '    global EXE_PATH\n'
    '    try:\n'
    '        pId = os.getpid()\n'
    '        subprocess.Popen(\n'
    "            f'{EXE_PATH} {dns_name} {pId} {exe_params}',\n"
    '            cwd=os.path.dirname(EXE_PATH),\n'
    '            creationflags=subprocess.CREATE_NEW_CONSOLE,\n'
    '        )\n'
    '    except Exception as ex:\n'
    '        critical_exit(f"ERR IN Start_benchmark --- {str(ex)}")\n'
)
new_func = (
    'def start_benchmark():\n'
    '    print("[wrapper] start_benchmark: skipped")\n'
    '    return\n'
)
patched = src.replace(old_func, new_func)

tmp_fd, tmp_path = tempfile.mkstemp(suffix='_init.py', dir=str(INIT_SCRIPT.parent))
os.close(tmp_fd)
tmp = Path(tmp_path)

try:
    tmp.write_text(patched, encoding='utf-8')

    env = os.environ.copy()
    env['BENCHMARK_STANDALONE'] = '1'

    r = subprocess.run(
        [sys.executable, str(tmp),
         str(WIDTH), str(HEIGHT), str(FPS),
         '--platform=Windows', '--vm=' + DNS_NAME],
        env=env,
    )
finally:
    try:
        tmp.unlink()
    except Exception:
        pass

print('[wrapper] init_script завершён, exit code: %d' % r.returncode)

# -- Шаг 2: автономный Cyberpunk-оркестратор ---------------------------------
print('\n[wrapper] Шаг 2: запуск cyberpunk_runner (config=%s)\n' % CYBERPUNK_CONFIG)

# Импортируем после init_script: на хосте девконтейнера импорт win32-секции упадёт,
# а на VM (Windows) — отработает корректно.
sys.path.insert(0, str(Path(__file__).parent))
from cyberpunk_runner import run_cyberpunk_benchmark

result = run_cyberpunk_benchmark(config=CYBERPUNK_CONFIG, iterations=2)

result_json = json.dumps(result, ensure_ascii=False, indent=2)
if OUTPUT_FILE is not None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(result_json, encoding='utf-8')
    print('[wrapper] Результат записан в %s' % OUTPUT_FILE)

print('\n[wrapper] Результаты Cyberpunk:')
print(result_json)
sys.exit(0)
