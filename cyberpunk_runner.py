"""
cyberpunk_runner.py — автономный запуск Cyberpunk 2077 benchmark.

Воспроизводит логику Benchmark.Gta.exe::CyberpunkBenchmarkService минимальным
набором действий — без Steam credentials, Google Sheets, SystemInfo.json и
зависимостей от benchmark-gta.zip.

Шаги:
  1. Скачать UserSettings.json с CDN в AppData игры
  2. Запустить Cyberpunk2077.exe с флагами -skipStartScreen -benchmark
  3. В фоне спамить ESC, чтобы пропустить заставки
  4. В фоне следить за окнами с ошибкой ("Отчёт об ошибке Cyberpunk 2077", "Ошибка")
  5. Ждать summary.json в benchmarkResults, парсить averageFps и пр.
  6. Убить процесс, повторить (1-й прогон — прогрев, результат не берём)

Использование:
    python cyberpunk_runner.py [--config vk|rt|2k] [--iterations N] [--output FILE]

Python 3.6 совместимый (на VM стоит Python36-32).
"""

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

# --- Конфиги Cyberpunk (CDN VK Play Cloud) ------------------------------------
CYBERPUNK_CONFIGS = {
    'vk': 'https://vkplaycloud.mrgcdn.ru/games/Configs/Cyberpunk/benchmark/UserSettings_120_test.json',
    'rt': 'https://vkplaycloud.mrgcdn.ru/Games/Configs/Cyberpunk/benchmark/UserSettings_RayTracing.json',
    '2k': 'https://vkplaycloud.mrgcdn.ru/Games/Configs/Cyberpunk/benchmark/UserSettings_2k.json',
}

# --- Пути и аргументы (взяты из CyberpunkBenchmarkService.cs / init_script) ---
# Запускаем строго через .lnk + ShellExecute — так делает оригинальный
# Process.Start({FileName=.lnk, UseShellExecute=true}). Double-.lnk имя файла —
# исторически так зашито и в init_script, и в pathToCyberpunkLnk константе exe.
CYBERPUNK_LNK      = Path(r'F:\launch\Steam\steamapps\common\Cyberpunk 2077\bin\x64\Cyberpunk2077.lnk.lnk')
CYBERPUNK_EXE      = Path(r'F:\launch\Steam\steamapps\common\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe')
USER_SETTINGS_DST  = Path(r'C:\Users\Gamer\AppData\Local\CD Projekt Red\Cyberpunk 2077\UserSettings.json')
RESULTS_DIR        = Path(r'C:\Users\gamer\Documents\CD Projekt Red\Cyberpunk 2077\benchmarkResults')
PROCESS_NAME       = 'Cyberpunk2077.exe'

# --- Тайминги (из CyberpunkBenchmarkService.cs + TimeOutWorker.cs) ------------
PROCESS_START_TIMEOUT_S = 4  * 60
BENCHMARK_TIMEOUT_S     = 8  * 60
ESC_DELAY_BEFORE_S      = 90
ESC_DURATION_S          = 120
ESC_INTERVAL_S          = 10
RESULT_POLL_INTERVAL_S  = 5
RESULT_SETTLE_S         = 10
INTER_ITER_DELAY_S      = 5
ANNOYANCE_CHECK_S       = 5

# Окна, при появлении которых прерываем итерацию (CyberpunkBenchmarkService.cs)
ERROR_WINDOWS = ['Отчёт об ошибке Cyberpunk 2077', 'Ошибка']

# Steam-окна, которые надо закрывать на лету, чтобы не украли фокус
# (StartupParameters.listWindowsNames -> FixService.StartWindowsFixAsync)
ANNOYANCE_WINDOWS = [
    'Специальные предложения', 'Список друзей',
    'Special Offers',          'Friends List',
]

# --- Win32 helpers ------------------------------------------------------------
# ESC отправляем строго scancode'ом через SendInput — InputSimulator.KeyPress
# в оригинале именно так и делает (dwFlags=KEYEVENTF_SCANCODE=0x08, wScan=0x01).
# Cyberpunk читает ввод через DirectInput/raw input, scancode обязателен.
INPUT_KEYBOARD     = 1
KEYEVENTF_KEYUP    = 0x0002
KEYEVENTF_SCANCODE = 0x0008
SCAN_ESCAPE        = 0x01   # DIK_ESCAPE из Common.Constants.ConfigButtons
WM_CLOSE           = 0x0010

ULONG_PTR = ctypes.c_size_t  # на x86 = 4 байта, на x64 = 8 — как IntPtr в .NET


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ('wVk',         wintypes.WORD),
        ('wScan',       wintypes.WORD),
        ('dwFlags',     wintypes.DWORD),
        ('time',        wintypes.DWORD),
        ('dwExtraInfo', ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ('dx',          wintypes.LONG),
        ('dy',          wintypes.LONG),
        ('mouseData',   wintypes.DWORD),
        ('dwFlags',     wintypes.DWORD),
        ('time',        wintypes.DWORD),
        ('dwExtraInfo', ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ('uMsg',    wintypes.DWORD),
        ('wParamL', wintypes.WORD),
        ('wParamH', wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [('ki', _KEYBDINPUT), ('mi', _MOUSEINPUT), ('hi', _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('u', _INPUT_UNION)]


if sys.platform == 'win32':
    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _user32.SendInput.argtypes  = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    _user32.SendInput.restype   = wintypes.UINT
    _user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.FindWindowW.restype  = wintypes.HWND
    _user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
    _user32.SendMessageW.restype  = ctypes.c_void_p
else:
    _user32 = None


def _send_scancode(scan, key_up):
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if key_up else 0)
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = _KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    sent = _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
    if sent == 0:
        raise ctypes.WinError(ctypes.get_last_error())


def _press_escape():
    # InputSimulator.KeyPress: KeyDown -> Thread.Sleep(100) -> KeyUp
    _send_scancode(SCAN_ESCAPE, key_up=False)
    time.sleep(0.1)
    _send_scancode(SCAN_ESCAPE, key_up=True)


def _find_window(title):
    return _user32.FindWindowW(None, title)


def _close_by_title(title):
    """ Эквивалент Utils.CloseByTitle: SendMessage(hWnd, WM_CLOSE). """
    hwnd = _user32.FindWindowW(None, title)
    if hwnd:
        _user32.SendMessageW(hwnd, WM_CLOSE, None, None)


# --- Process helpers ----------------------------------------------------------
def _process_running(name):
    r = subprocess.run(
        ['tasklist', '/FI', 'IMAGENAME eq ' + name, '/NH'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    return name.lower() in r.stdout.lower()


def _kill_process(name):
    subprocess.run(
        ['taskkill', '/F', '/IM', name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


# --- Логика -------------------------------------------------------------------
def log(msg):
    print('[cp] ' + msg, flush=True)


def download_user_settings(config):
    url = CYBERPUNK_CONFIGS[config]
    USER_SETTINGS_DST.parent.mkdir(parents=True, exist_ok=True)
    log('Качаю UserSettings.json ({0}): {1}'.format(config, url))
    urllib.request.urlretrieve(url, str(USER_SETTINGS_DST))
    log('  -> ' + str(USER_SETTINGS_DST))


def delete_old_results():
    if RESULTS_DIR.exists():
        log('Удаляю старые результаты: ' + str(RESULTS_DIR))
        shutil.rmtree(str(RESULTS_DIR), ignore_errors=True)


def launch_game():
    # Оригинал: Process.Start({FileName=.lnk, UseShellExecute=true}) — то есть
    # ShellExecuteEx по ярлыку. В Python это os.startfile().
    if not CYBERPUNK_LNK.exists():
        raise FileNotFoundError(
            'Cyberpunk2077.lnk.lnk не найден: ' + str(CYBERPUNK_LNK) +
            ' (его должен создать init_script_reconstructed.initialize_cyberpunk_shortcut)'
        )
    log('Запускаю через ShellExecute: ' + str(CYBERPUNK_LNK))
    os.startfile(str(CYBERPUNK_LNK))


def wait_for_process(timeout_s):
    log('Жду появления процесса {0} (timeout {1}s)'.format(PROCESS_NAME, timeout_s))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _process_running(PROCESS_NAME):
            log('  -> процесс найден')
            return
        time.sleep(1)
    raise TimeoutError('{0} не появился за {1}s'.format(PROCESS_NAME, timeout_s))


def escape_spammer(stop_event):
    """ Ждём 90с, потом спамим ESC каждые 10с в течение 2 минут. """
    if stop_event.wait(ESC_DELAY_BEFORE_S):
        return
    log('Начинаю отправку ESC для пропуска заставки')
    end = time.monotonic() + ESC_DURATION_S
    while time.monotonic() < end and not stop_event.is_set():
        _press_escape()
        if stop_event.wait(ESC_INTERVAL_S):
            return


def check_error_windows_post_mortem():
    """ Пост-мортем: после завершения процесса Cyberpunk оригинал ждёт 5с и
    проверяет наличие окон "Отчёт об ошибке Cyberpunk 2077" / "Ошибка".
    Сделано чисто для диагностики — не прерываем тест, просто логируем. """
    found = []
    for title in ERROR_WINDOWS:
        if _find_window(title):
            found.append(title)
    if found:
        log('!! Обнаружены окна с ошибкой (пост-мортем): ' + ', '.join('"' + t + '"' for t in found))
    return found


def annoyance_closer(stop_event):
    """ Эквивалент FixService.StartWindowsFixAsync: каждые 5 секунд
    закрывает Steam-попапы (Specials/Friends List), если выскочат во время
    бенчмарка, иначе фокус уйдёт с игры и измерение испортится. """
    while not stop_event.is_set():
        for title in ANNOYANCE_WINDOWS:
            try:
                _close_by_title(title)
            except Exception:
                pass
        if stop_event.wait(ANNOYANCE_CHECK_S):
            return


def wait_for_summary_json(timeout_s):
    log('Жду summary.json в ' + str(RESULTS_DIR))
    deadline = time.monotonic() + timeout_s

    # 1. ждём появления папки
    while time.monotonic() < deadline and not RESULTS_DIR.exists():
        time.sleep(RESULT_POLL_INTERVAL_S)
    if not RESULTS_DIR.exists():
        raise TimeoutError('Папка ' + str(RESULTS_DIR) + ' так и не появилась')

    # 2. ждём появления .json в подпапке
    json_file = None
    while time.monotonic() < deadline and json_file is None:
        for d in RESULTS_DIR.iterdir():
            if not d.is_dir():
                continue
            jsons = list(d.glob('*.json'))
            if jsons:
                json_file = jsons[0]
                break
        if json_file is None:
            time.sleep(RESULT_POLL_INTERVAL_S)
    if json_file is None:
        raise TimeoutError('summary.json не появился')

    log('Найден ' + str(json_file) + ', жду пока заполнится')

    # 3. ждём непустого содержимого, затем 10с settle (как в оригинале)
    while time.monotonic() < deadline:
        if json_file.stat().st_size > 0:
            time.sleep(RESULT_SETTLE_S)
            return json_file
        time.sleep(RESULT_POLL_INTERVAL_S)
    raise TimeoutError(str(json_file) + ' остался пустым')


def parse_summary(path):
    raw = json.loads(path.read_text(encoding='utf-8'))
    data = raw.get('Data', {})
    return {
        'averageFps':        data.get('averageFps'),
        'minFps':            data.get('minFps'),
        'maxFps':            data.get('maxFps'),
        'gpuName':           data.get('gpuName'),
        'rayTracingEnabled': data.get('rayTracingEnabled'),
        'DLSSEnabled':       data.get('DLSSEnabled'),
        'source_file':       str(path),
    }


def run_single_iteration(i, total):
    log('')
    log('=== Итерация {0}/{1} ==='.format(i, total))
    delete_old_results()
    launch_game()

    stop_event = threading.Event()
    threads = [
        threading.Thread(target=escape_spammer,   args=(stop_event,), daemon=True),
        threading.Thread(target=annoyance_closer, args=(stop_event,), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        wait_for_process(PROCESS_START_TIMEOUT_S)
        summary = wait_for_summary_json(BENCHMARK_TIMEOUT_S)
        return summary
    finally:
        stop_event.set()
        log('Убиваю процесс ' + PROCESS_NAME)
        _kill_process(PROCESS_NAME)
        # Оригинал: после WaitForExit+5с проверяет окна с ошибкой
        time.sleep(INTER_ITER_DELAY_S)
        check_error_windows_post_mortem()


def run_cyberpunk_benchmark(config='vk', iterations=2):
    if config not in CYBERPUNK_CONFIGS:
        raise ValueError('Unknown config: ' + config + ', choose from ' + ', '.join(CYBERPUNK_CONFIGS))
    if not CYBERPUNK_EXE.exists():
        raise FileNotFoundError('Cyberpunk2077.exe не найден: ' + str(CYBERPUNK_EXE))
    if not CYBERPUNK_LNK.exists():
        raise FileNotFoundError(
            'Cyberpunk2077.lnk.lnk не найден: ' + str(CYBERPUNK_LNK) +
            ' — нужен прогон init_script_reconstructed для создания ярлыка.'
        )

    download_user_settings(config)

    summary_path = None
    for i in range(1, iterations + 1):
        summary_path = run_single_iteration(i, iterations)

    return parse_summary(summary_path)


def main():
    p = argparse.ArgumentParser(description='Standalone Cyberpunk 2077 benchmark runner')
    p.add_argument('--config', choices=sorted(CYBERPUNK_CONFIGS), default='vk',
                   help='Какой UserSettings.json применять (default: vk = 120fps test)')
    p.add_argument('--iterations', type=int, default=2,
                   help='Сколько прогонов (1-й = прогрев, default: 2)')
    p.add_argument('--output', type=Path, default=None,
                   help='Куда записать JSON результата (default: stdout)')
    args = p.parse_args()

    result = run_cyberpunk_benchmark(config=args.config, iterations=args.iterations)
    out_text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(out_text, encoding='utf-8')
        log('Результат записан в ' + str(args.output))
    else:
        log('Результат:')
        print(out_text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
