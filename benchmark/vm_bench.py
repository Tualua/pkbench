"""
vm_bench.py — точка входа бенчмарка на VM.

Запускается через PsExec под gamer'ом (cmd.bat → launcher.bat → этот скрипт).
Один python-процесс делает всё: shader prewarm, NVENC load, Cyberpunk runner,
status-writer. До рефактора было три скрипта (run_via_ga + run_benchmark +
cyberpunk_runner) — теперь один.

CLI:
    python vm_bench.py [config] [steam_user] [steam_pass] [only_wukong]
        config: vk (default, 120fps test) | rt (RayTracing) | 2k
        steam_user/steam_pass: для опционального Wukong-таска после Cyberpunk.
        Пустые строки → Wukong пропускается.
        only_wukong: '1' → пропустить Cyberpunk, гнать только Wukong (debug-режим).
                     Wukong-фейл тогда становится фатальным (бенч пустой).

NVENC-нагрузка запускается ВСЕГДА. Если ffmpeg не стартовал ИЛИ умер посреди
бенча — exit_code != 0, бенч считается неуспешным (без encoder-нагрузки
результат не репрезентативен).

Wukong-таск в обычном режиме (только Cyberpunk + опционально Wukong) фейлит
не валит общий бенч — Cyberpunk-результаты уже собраны, Wukong-ошибки уходят
в status.wukong_error. В режиме only_wukong Wukong-фейл = фатал бенча.

Артефакты в C:\\benchmark\\:
    last_run.log     — stdout/stderr всего прогона (через os.dup2 на fd 1/2)
    last_result.json — JSON результата (только при exit_code=0)
    last_status.json — статус прогона (exit_code, duration, ...) — ПИШЕТСЯ ВСЕГДА
    last_ffmpeg.log  — stderr ffmpeg

last_status.json — это сигнал хосту, что прогон закончился (хост поллит его
наличие через guest-file-open).

NVENC fidelity:
    Source: synthetic lavfi testsrc, НЕ ddagrab. Production использует
    DX-hook injection (SharedCapture_x64.dll), что невоспроизводимо без
    GameServer-стека. Encoder workload (~98% GPU нагрузки от стрима) идентичен.
    ddagrab + Cyberpunk на VK-конфиге = DXGI_ERROR_ACCESS_LOST в fullscreen.
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from ctypes import wintypes
from pathlib import Path

# ── Пути / артефакты ─────────────────────────────────────────────────────────
BENCH_DIR    = Path(r'C:\benchmark')
LOG_FILE     = BENCH_DIR / 'last_run.log'
RESULT_FILE  = BENCH_DIR / 'last_result.json'
STATUS_FILE  = BENCH_DIR / 'last_status.json'
FFMPEG_LOG   = BENCH_DIR / 'last_ffmpeg.log'
FFMPEG_EXE   = BENCH_DIR / 'ffmpeg.exe'

CYBERPUNK_LNK     = Path(r'F:\launch\Steam\steamapps\common\Cyberpunk 2077\bin\x64\Cyberpunk2077.lnk.lnk')
CYBERPUNK_EXE     = Path(r'F:\launch\Steam\steamapps\common\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe')
USER_SETTINGS_DST = Path(r'C:\Users\Gamer\AppData\Local\CD Projekt Red\Cyberpunk 2077\UserSettings.json')
RESULTS_DIR       = Path(r'C:\Users\gamer\Documents\CD Projekt Red\Cyberpunk 2077\benchmarkResults')

CYBERPUNK_LNK_TARGET = r'F:\launch\Steam\steamapps\common\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe'
CYBERPUNK_LNK_ARGS   = '-skipStartScreen -benchmark -watchdogTimeout 180'

# ── Конфиги Cyberpunk (CDN VK Play Cloud) ────────────────────────────────────
CYBERPUNK_CONFIGS = {
    'vk': 'https://vkplaycloud.mrgcdn.ru/games/Configs/Cyberpunk/benchmark/UserSettings_120_test.json',
    'rt': 'https://vkplaycloud.mrgcdn.ru/Games/Configs/Cyberpunk/benchmark/UserSettings_RayTracing.json',
    '2k': 'https://vkplaycloud.mrgcdn.ru/Games/Configs/Cyberpunk/benchmark/UserSettings_2k.json',
}

# ── Тайминги (из CyberpunkBenchmarkService.cs / TimeOutWorker.cs) ────────────
PROCESS_NAME            = 'Cyberpunk2077.exe'
PROCESS_START_TIMEOUT_S = 4 * 60
BENCHMARK_TIMEOUT_S     = 8 * 60
ESC_DELAY_BEFORE_S      = 90
ESC_DURATION_S          = 120
ESC_INTERVAL_S          = 10
RESULT_POLL_INTERVAL_S  = 5
RESULT_SETTLE_S         = 10
INTER_ITER_DELAY_S      = 5
ANNOYANCE_CHECK_S       = 5

ITERATIONS = 2   # 1-й = прогрев, результат берём со 2-го

ERROR_WINDOWS = ['Отчёт об ошибке Cyberpunk 2077', 'Ошибка']
ANNOYANCE_WINDOWS = [
    'Специальные предложения', 'Список друзей',
    'Special Offers',          'Friends List',
]

# ── Steam UI-automation (scancodes, FindWindow, registry) ────────────────────
# Порт PassAuthorization из Common.Steam.dll / Common.WinApi.InputSimulator —
# флаг `steam.exe -login user pass` устарел в новых Steam, теперь только UI.
# Алгоритм: kill_steam → clear registry+loginusers.vdf → launch steam.exe →
# wait auth-window (class SDL_app) → SetForeground → english kb-layout →
# scancode-type login → Tab → scancode-type pass → Return → poll ActiveUser.

# Флаги при каждом запуске steam.exe. -silent скрывает основное окно (auth
# popup всё равно появится когда нужен ввод), nofriendsui/nochatui убирают
# вспомогательные оверлеи. На новых Steam часть эффекта может быть ограничена,
# но не мешает UI-automation и не вредит.
STEAM_LAUNCH_FLAGS = ['-silent', '-nofriendsui', '-nochatui']

STEAM_AUTH_WINDOW_CLASS = 'SDL_app'   # Steam UI на SDL2 — все окна имеют этот класс
STEAM_AUTH_CAPTIONS     = ['Войти в Steam', 'Sign in to Steam']
STEAM_AUTH_WAIT_S       = 120         # таймаут появления auth-окна
STEAM_LOGIN_POLL_S      = 1
STEAM_ACTIVEUSER_WAIT_S = 120         # после ввода — ждать пока ActiveUser != 0

# NB: раньше тут была логика click'а на install dialog (Apex-style Tab*5+Enter).
# Не работало — UI Steam 2025-2026 поменялся, фокус приземляется на "Отмена",
# либо вообще не на нужный диалог. Заменили на подкладку appmanifest ДО старта
# Steam: при scan библиотеки Steam видит manifest + файлы на диске, applaunch
# работает без диалога. См. ensure_wukong_manifest() / steam_login_with_ui_automation.

# DIK scancode (set 1) — из Common.WinApi.ConfigButtons.getButtonsKey().
# Только то, что нам нужно для ASCII login/pass (без F-ключей, NumPad).
_DIK_LOOKUP = {
    ' ': 57, '`': 41,
    '-': 12, '=': 13, '[': 26, ']': 27, ';': 39, "'": 40,
    '\\': 43, ',': 51, '.': 52, '/': 53,
    '0': 11, '1': 2, '2': 3, '3': 4, '4': 5, '5': 6,
    '6': 7, '7': 8, '8': 9, '9': 10,
    'a': 30, 'b': 48, 'c': 46, 'd': 32, 'e': 18, 'f': 33,
    'g': 34, 'h': 35, 'i': 23, 'j': 36, 'k': 37, 'l': 38,
    'm': 50, 'n': 49, 'o': 24, 'p': 25, 'q': 16, 'r': 19,
    's': 31, 't': 20, 'u': 22, 'v': 47, 'w': 17, 'x': 45,
    'y': 21, 'z': 44,
}
# Shifted ASCII → base key (которая в _DIK_LOOKUP). Используется в write_line
# когда нужно нажать символ через Shift+base. Соответствует таблице исключений
# в InputSimulator.WriteLine.
_SHIFT_BASE = {
    '~': '`', '!': '1', '@': '2', '#': '3', '$': '4',
    '%': '5', '^': '6', '&': '7', '*': '8', '(': '9',
    ')': '0', '_': '-', '+': '=', '{': '[', '}': ']',
    ':': ';', '|': '\\', '<': ',', '>': '.', '?': '/',
    '"': "'",
}
_DIK_TAB    = 15
_DIK_RETURN = 28
_DIK_RSHIFT = 54


def _dik_for(ch):
    """Scancode для одного символа (lower-case или цифры/спец). Для uppercase
    автоматически даёт DIK lower-case + shift добавляет вызывающий."""
    return _DIK_LOOKUP.get(ch.lower())


def _press_scan(scan):
    """KeyDown + small delay + KeyUp. Для не-Cyberpunk ESC."""
    _send_scancode(scan, key_up=False)
    time.sleep(0.1)
    _send_scancode(scan, key_up=True)


def write_line(text):
    """Послать строку через SendInput с scancode'ами (DIK). Каждый символ
    как KeyDown+KeyUp, спец-символы / uppercase — с зажатым RShift.

    Окно должно быть уже в foreground — клавиши идут глобально, не на hwnd.
    """
    for ch in text:
        # Определяем base-символ + нужен ли shift
        if ch in _SHIFT_BASE:
            need_shift = True
            base = _SHIFT_BASE[ch]
        elif ch.isupper():
            need_shift = True
            base = ch.lower()
        else:
            need_shift = False
            base = ch

        scan = _DIK_LOOKUP.get(base)
        if scan is None:
            log('  WARN: scancode не найден для "{0}" (chr={1}), пропуск'.format(ch, ord(ch)))
            continue

        if need_shift:
            time.sleep(0.05)
            _send_scancode(_DIK_RSHIFT, key_up=False)
        time.sleep(0.05)
        _press_scan(scan)
        if need_shift:
            time.sleep(0.05)
            _send_scancode(_DIK_RSHIFT, key_up=True)


def _find_window_by_class(class_name, caption):
    """FindWindowW. Возвращает HWND (int) или 0.
    Отдельно от _find_window(title) (cyberpunk error-windows) — иначе name
    collision: Python поздним определением перетирал бы эту двух-аргументную."""
    hwnd = _user32.FindWindowW(class_name, caption)
    return int(hwnd) if hwnd else 0


def find_steam_auth_window(timeout_s):
    """Поллим FindWindow по SDL_app + одному из CAPTIONS. Возвращает hwnd
    или 0 если за timeout не появилось."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for caption in STEAM_AUTH_CAPTIONS:
            hwnd = _find_window_by_class(STEAM_AUTH_WINDOW_CLASS, caption)
            if hwnd:
                log('Auth-окно найдено: class="{0}" caption="{1}" hwnd={2}'.format(
                    STEAM_AUTH_WINDOW_CLASS, caption, hwnd))
                return hwnd
        time.sleep(STEAM_LOGIN_POLL_S)
    return 0


def _activate_window(hwnd):
    """SetForegroundWindow + BringWindowToTop + ShowWindow(SW_SHOW). Несколько
    апикей-вызовов, потому что иногда один не срабатывает (Windows focus rules)."""
    # Ленивая привязка — функции вызовутся только на Windows.
    user32 = _user32
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype  = wintypes.BOOL
    user32.BringWindowToTop.argtypes    = [wintypes.HWND]
    user32.BringWindowToTop.restype     = wintypes.BOOL
    user32.ShowWindow.argtypes          = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype           = wintypes.BOOL
    SW_SHOW = 5
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.ShowWindow(hwnd, SW_SHOW)
    time.sleep(0.3)


def _set_english_keyboard(hwnd):
    """LoadKeyboardLayout("00000409", KLF_ACTIVATE=1) → WM_INPUTLANGCHANGEREQUEST.
    Чтобы латиница не превратилась в кириллицу при русской раскладке гостя."""
    user32 = _user32
    user32.LoadKeyboardLayoutW.argtypes = [wintypes.LPCWSTR, wintypes.UINT]
    user32.LoadKeyboardLayoutW.restype  = ctypes.c_void_p
    KLF_ACTIVATE = 0x00000001
    lparam = user32.LoadKeyboardLayoutW('00000409', KLF_ACTIVATE)
    WM_INPUTLANGCHANGEREQUEST = 0x0050
    user32.SendMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, None, lparam)


# ── Steam Guard 2FA auto-input (IMAP) ────────────────────────────────────────
# Порт MailReceiver.GetSteamCodes из Common.dll. Логика 1-в-1:
#   1. IMAP SELECT INBOX
#   2. SEARCH UNSEEN (только непрочитанные)
#   3. Для каждого письма (от нового к старому):
#       - Первая строка письма должна содержать наш Steam-логин (lowercase)
#       - Найти строку, оканчивающуюся на "Код доступа" / "Login Code"
#       - Следующая строка после неё — 5-символьный код
#   4. Возвращаем первый найденный код, без него — None
# Перед login делаем mark_all_unseen_as_seen чтобы не зацепить старые письма.

STEAM_GUARD_ATTEMPTS         = 5      # повторов IMAP-fetch
STEAM_GUARD_INTERVAL_S       = 15     # пауза между fetch-попытками
STEAM_GUARD_AFTER_INPUT_WAIT = 30     # после ввода кода — ждать ActiveUser != 0
STEAM_GUARD_LINE_MARKERS     = ('Код доступа', 'Login Code')


def _imap_connect(host, user, pwd):
    """SSL IMAP-подключение. raises на ошибке — вверх к steam_login_with_ui."""
    import imaplib
    M = imaplib.IMAP4_SSL(host, 993)
    M.login(user, pwd)
    M.select('INBOX')
    return M


def _extract_text_body(msg):
    """text/plain payload из email.message, декодированный в str. None если нет."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or 'utf-8'
                try:
                    return payload.decode(charset, errors='replace')
                except Exception:
                    pass
        return None
    payload = msg.get_payload(decode=True)
    if payload is None:
        return None
    charset = msg.get_content_charset() or 'utf-8'
    try:
        return payload.decode(charset, errors='replace')
    except Exception:
        return None


def mark_all_unseen_as_seen(host, user, pwd):
    """Чистим inbox от старых непрочитанных Steam Guard писем, чтобы потом
    не зацепить устаревший код. Тихо игнорируем ошибки — не критично."""
    try:
        M = _imap_connect(host, user, pwd)
    except Exception as ex:
        log('  WARN: IMAP connect упал: {0}'.format(ex))
        return
    try:
        typ, data = M.search(None, 'UNSEEN')
        if typ == 'OK' and data and data[0]:
            ids = data[0].split()
            for uid in ids:
                M.store(uid, '+FLAGS', '\\Seen')
            log('  пометил {0} старых непрочитанных писем как seen'.format(len(ids)))
    except Exception as ex:
        log('  WARN: mark_seen упал: {0}'.format(ex))
    finally:
        try:
            M.close()
        except Exception:
            pass
        try:
            M.logout()
        except Exception:
            pass


def fetch_steam_guard_code(host, user, pwd, steam_account):
    """Подключиться к IMAP, среди UNSEEN найти письмо где первая строка
    содержит steam_account, в нём строку перед "Код доступа"/"Login Code",
    взять следующую строку — 5-символьный код. Возвращает str или None."""
    import email
    try:
        M = _imap_connect(host, user, pwd)
    except Exception as ex:
        log('  WARN: IMAP connect упал: {0}'.format(ex))
        return None
    try:
        typ, data = M.search(None, 'UNSEEN')
        if typ != 'OK' or not data or not data[0]:
            return None
        ids = data[0].split()
        # От нового к старому (Steam Guard коды быстро устаревают, берём свежие)
        for uid in reversed(ids):
            typ, msg_data = M.fetch(uid, '(RFC822)')
            if typ != 'OK' or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            msg = email.message_from_bytes(bytes(raw))
            body = _extract_text_body(msg)
            if not body:
                continue
            lines = [ln for ln in body.splitlines() if ln.strip()]
            if not lines:
                continue
            # Первая строка должна содержать Steam-логин (точно как в оригинале).
            if steam_account.lower() not in lines[0].lower():
                continue
            # Найти строку с маркером, взять следующую за ней.
            for i, line in enumerate(lines):
                stripped = line.rstrip()
                if any(stripped.endswith(m) for m in STEAM_GUARD_LINE_MARKERS):
                    if i + 1 < len(lines):
                        code = lines[i + 1].strip()
                        if len(code) == 5:
                            return code
                    break
        return None
    finally:
        try:
            M.close()
        except Exception:
            pass
        try:
            M.logout()
        except Exception:
            pass


def insert_steam_guard_code(code):
    """После login+pass+Enter Steam показывает в том же auth-окне поле кода с
    фокусом на нём. Просто WriteLine + Enter — поле уже сфокусировано Steam'ом."""
    log('Ввожу Steam Guard код: {0}'.format(code))
    time.sleep(0.5)
    write_line(code)
    time.sleep(0.3)
    _press_scan(_DIK_RETURN)


# ── Steam registry / loginusers.vdf cleanup ──────────────────────────────────
STEAM_LOGINUSERS_VDF = Path(r'F:\launch\Steam\config\loginusers.vdf')
STEAM_APPS_DIR       = Path(r'F:\launch\Steam\steamapps')

# 1-в-1 как vm_lib/steam_pk.py::steam_copy_manifest: на VM от GameServer лежит
# pre-prepared snapshot manifest'а в steamapps/manifests/. Steam при applaunch
# показывает install dialog только потому что нужный файл лежит не там, где
# смотрит Steam (steamapps/), а на шаг глубже (steamapps/manifests/). Копируем
# snapshot → активное место, обнуляем LastOwner (Steam при следующем login
# сам впишет нового владельца) — точно как делает steam_edit_manifest.editor().
WUKONG_MANIFEST_PATH   = STEAM_APPS_DIR / 'appmanifest_3132990.acf'
WUKONG_MANIFEST_SOURCE = STEAM_APPS_DIR / 'manifests' / 'appmanifest_3132990.acf'


def ensure_wukong_manifest():
    """Эквивалент steam_copy_manifest(lib_path, 3132990) из vm_lib/steam_pk.py.

    Если в активном месте manifest уже есть — не трогаем (Steam мог сам его
    дополнить полями InstalledDepots/buildid после реальной установки).
    Иначе — копируем pre-prepared snapshot из steamapps/manifests/, обнуляя
    LastOwner regex'ом (как steam_edit_manifest.editor).

    Должно вызываться ПЕРЕД launch'ом steam.exe — Steam сканит steamapps/
    только при старте.
    """
    if WUKONG_MANIFEST_PATH.exists():
        log('Wukong manifest уже в steamapps/, не трогаю')
        return
    if not WUKONG_MANIFEST_SOURCE.exists():
        log('  WARN: pre-prepared snapshot {0} не найден на VM. Steam покажет '
            'install dialog при applaunch.'.format(WUKONG_MANIFEST_SOURCE))
        return
    log('Копирую Wukong manifest: {0} → {1}'.format(
        WUKONG_MANIFEST_SOURCE, WUKONG_MANIFEST_PATH))
    text = WUKONG_MANIFEST_SOURCE.read_text(encoding='utf-8')
    text = re.sub(r'("LastOwner"\s*)"\d+"', r'\1""', text)
    WUKONG_MANIFEST_PATH.write_text(text, encoding='utf-8')


def _read_active_user():
    """HKCU\\Software\\Valve\\Steam\\ActiveProcess::ActiveUser → int или 0."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Valve\Steam\ActiveProcess') as key:
            val, _ = winreg.QueryValueEx(key, 'ActiveUser')
            try:
                return int(val)
            except Exception:
                return 0
    except Exception:
        return 0


def clear_steam_login_state():
    """Стираем закешированный auto-login: registry ActiveUser=0, AutoLoginUser='',
    + удаляем loginusers.vdf. Это форсирует Steam показать auth-окно при
    следующем запуске — нам нужно для UI-automation. Эквивалент
    BaseSteamClient.DeleteSteamLoginRegistry()."""
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r'Software\Valve\Steam\ActiveProcess',
                                0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, 'ActiveUser', 0, winreg.REG_DWORD, 0)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r'Software\Valve\Steam',
                                0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, 'AutoLoginUser', 0, winreg.REG_SZ, '')
    except Exception as ex:
        log('  WARN: clear registry упал: {0}'.format(ex))
    if STEAM_LOGINUSERS_VDF.exists():
        try:
            STEAM_LOGINUSERS_VDF.unlink()
        except Exception as ex:
            log('  WARN: не удалил {0}: {1}'.format(STEAM_LOGINUSERS_VDF, ex))


# ── Wukong (опциональный второй бенч) ────────────────────────────────────────
# Black Myth Wukong Benchmark Tool — отдельный бесплатный standalone (НЕ сама
# игра). Free product, активируется одним кликом на странице Steam Store.
STEAM_EXE                = Path(r'F:\launch\Steam\steam.exe')
WUKONG_STEAM_APPID       = 3132990
WUKONG_PROCESS           = 'b1-Win64-Shipping.exe'
WUKONG_SUBPROCESS        = 'b1_benchmark.exe'
WUKONG_RESULTS_DIR       = Path(r'C:\Users\gamer\AppData\Local\Temp\b1\BenchMarkHistory\Tool')
WUKONG_USER_SETTINGS_INI = Path(r'F:\launch\Steam\steamapps\common\Black Myth Wukong Benchmark Tool\b1\Saved\Config\Windows\GameUserSettings.ini')
WUKONG_CONFIG_URL        = 'https://vkplaycloud.mrgcdn.ru/games/Configs/Black_Myth_Wukong_Benchmark_Tool/GameUserSettings.ini'

WUKONG_PROCESS_START_TIMEOUT_S = 5 * 60
WUKONG_BENCH_TIMEOUT_S         = 12 * 60   # сам бенч ~3-5 мин, запас на UE-стартап
WUKONG_APPLAUNCH_WAIT_S        = 30        # после applaunch ждём процесса
WUKONG_RESULT_POLL_S           = 5
WUKONG_RESULT_SETTLE_S         = 5

# ── NVENC параметры (из лога GameServer, сессия 64795392) ────────────────────
NVENC_BITRATE_KBPS = 25000
NVENC_FPS          = 60
NVENC_WARMUP_S     = 2

FFMPEG_ARGS = [
    '-loglevel', 'error',
    '-stats',
    '-stats_period', '10',
    # -re КРИТИЧЕН: без него lavfi отдаёт фреймы as-fast-as-possible, NVENC
    # encoder гонит ~200fps (3.34x) — в 3 раза тяжелее production. С -re ffmpeg
    # pacит input на native 60fps, encoder работает ровно как в production.
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

# ── Win32: SendInput (ESC) + FindWindow/WM_CLOSE (закрытие окон) ─────────────
# ESC через SendInput со SCANCODE — это то же, что делает InputSimulator.KeyPress
# в Benchmark.Gta.exe (dwFlags=KEYEVENTF_SCANCODE=0x08, wScan=0x01). Cyberpunk
# читает ввод через DirectInput/raw input — scancode обязателен, vkey игнорится.
INPUT_KEYBOARD     = 1
KEYEVENTF_KEYUP    = 0x0002
KEYEVENTF_SCANCODE = 0x0008
SCAN_ESCAPE        = 0x01
WM_CLOSE           = 0x0010

ULONG_PTR = ctypes.c_size_t  # x86=4, x64=8 — как IntPtr в .NET


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
    _user32.SendInput.argtypes    = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    _user32.SendInput.restype     = wintypes.UINT
    _user32.FindWindowW.argtypes  = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.FindWindowW.restype   = wintypes.HWND
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
    _send_scancode(SCAN_ESCAPE, key_up=False)
    time.sleep(0.1)
    _send_scancode(SCAN_ESCAPE, key_up=True)


def _find_window(title):
    return _user32.FindWindowW(None, title)


def _close_by_title(title):
    hwnd = _user32.FindWindowW(None, title)
    if hwnd:
        _user32.SendMessageW(hwnd, WM_CLOSE, None, None)


# ── Process helpers ──────────────────────────────────────────────────────────
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


# ── Логирование (после dup2 идёт в last_run.log) ─────────────────────────────
def log(msg):
    print('[bench] ' + msg, flush=True)


# ── Setup: ярлык Cyberpunk + DX shaders prewarm ──────────────────────────────
def setup_cyberpunk_shortcut():
    """Создаёт Cyberpunk2077.lnk.lnk с нужными флагами.

    Имя с двойным .lnk — историческое: и оригинальный init_script GameServer,
    и Benchmark.Gta.exe (CyberpunkBenchmarkService.pathToCyberpunkLnk) знают
    игру именно по этому пути. Cyberpunk_runner запускает через ShellExecute
    по этому ярлыку — флаги -skipStartScreen/-benchmark должны попасть в exe.
    """
    log('Создаю ярлык Cyberpunk: ' + str(CYBERPUNK_LNK))
    if CYBERPUNK_LNK.exists():
        try:
            CYBERPUNK_LNK.unlink()
        except Exception:
            pass
    # WScript.Shell.CreateShortcut — стандартный способ создания .lnk на Windows
    # без зависимости от pywin32 (Dispatch есть в pkinit через win32com.client,
    # но у нас нет гарантии что pywin32 импортнётся, так что зовём через WSH
    # напрямую — он есть в любой Windows).
    # Импорт внутри функции, не на module-level: vm_bench.py парсится и на Linux
    # для синтакс-чека (pkbench.py deploy его читает как файл), а win32com там нет.
    from win32com.client import Dispatch
    shell = Dispatch('WScript.Shell')
    shortcut = shell.CreateShortcut(str(CYBERPUNK_LNK))
    shortcut.TargetPath = CYBERPUNK_LNK_TARGET
    shortcut.Arguments  = CYBERPUNK_LNK_ARGS
    shortcut.Save()


def prewarm_dx_shaders():
    """Прогрев DX shader cache через pkinit.GameShaders.

    pkinit — production-модуль (лежит в site-packages Python36-32, ставит
    GameServer при подготовке VM). Импортируется here, не на уровне модуля,
    чтобы синтаксический парс vm_bench.py не зависел от его наличия.
    На пустом VM без pkinit — пишем warning и продолжаем (без прогрева
    первая итерация будет с shader-compilation hitch'ами; вторая чистая).
    """
    try:
        from pkinit import GameShaders
    except Exception as ex:
        log('  WARN: pkinit недоступен ({0}), пропускаю shader prewarm'.format(ex))
        return
    try:
        log('Прогрев DX shaders (pkinit.GameShaders.setup_dx_shaders Cyberpunk)...')
        GameShaders.setup_dx_shaders('Cyberpunk')
        log('  -> готово')
    except Exception as ex:
        log('  WARN: shader prewarm упал: {0}'.format(ex))


# ── NVENC load ───────────────────────────────────────────────────────────────
def start_nvenc_load():
    """Запустить ffmpeg-нагрузку. Возвращает (Popen, error_msg)."""
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


# ── Cyberpunk runner (бывший cyberpunk_runner.py) ────────────────────────────
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
    if not CYBERPUNK_LNK.exists():
        raise FileNotFoundError(
            'Cyberpunk2077.lnk.lnk не найден: ' + str(CYBERPUNK_LNK) +
            ' (должен быть создан setup_cyberpunk_shortcut)'
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
    """90с ждём, потом каждые 10с шлём ESC в течение 2 минут."""
    if stop_event.wait(ESC_DELAY_BEFORE_S):
        return
    log('Начинаю отправку ESC для пропуска заставки')
    end = time.monotonic() + ESC_DURATION_S
    while time.monotonic() < end and not stop_event.is_set():
        _press_escape()
        if stop_event.wait(ESC_INTERVAL_S):
            return


def annoyance_closer(stop_event):
    """Каждые 5с закрывает Steam-попапы, если выскочат. Аналог
    FixService.StartWindowsFixAsync в Benchmark.Gta.exe."""
    while not stop_event.is_set():
        for title in ANNOYANCE_WINDOWS:
            try:
                _close_by_title(title)
            except Exception:
                pass
        if stop_event.wait(ANNOYANCE_CHECK_S):
            return


def check_error_windows_post_mortem():
    """Пост-мортем: после kill'а Cyberpunk проверяем error-окна — только для
    диагностики, не валим прогон."""
    found = []
    for title in ERROR_WINDOWS:
        if _find_window(title):
            found.append(title)
    if found:
        log('!! Обнаружены окна с ошибкой (пост-мортем): ' + ', '.join('"' + t + '"' for t in found))
    return found


def wait_for_summary_json(timeout_s):
    log('Жду summary.json в ' + str(RESULTS_DIR))
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline and not RESULTS_DIR.exists():
        time.sleep(RESULT_POLL_INTERVAL_S)
    if not RESULTS_DIR.exists():
        raise TimeoutError('Папка ' + str(RESULTS_DIR) + ' так и не появилась')

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
        time.sleep(INTER_ITER_DELAY_S)
        check_error_windows_post_mortem()


def run_cyberpunk_benchmark(config):
    if config not in CYBERPUNK_CONFIGS:
        raise ValueError('Unknown config: ' + config)
    if not CYBERPUNK_EXE.exists():
        raise FileNotFoundError('Cyberpunk2077.exe не найден: ' + str(CYBERPUNK_EXE))

    setup_cyberpunk_shortcut()
    prewarm_dx_shaders()
    download_user_settings(config)

    summary_path = None
    for i in range(1, ITERATIONS + 1):
        summary_path = run_single_iteration(i, ITERATIONS)

    return parse_summary(summary_path)


# ── Wukong runner ────────────────────────────────────────────────────────────
def kill_steam():
    """Жёстко гасим Steam, чтобы потом залогиниться под нужным аккаунтом.
    Если был залогинен другим — иначе -login no-op."""
    for proc in ('steam.exe', 'steamwebhelper.exe'):
        subprocess.run(['taskkill', '/F', '/IM', proc],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)


def download_wukong_config():
    """Кладём GameUserSettings.ini с CDN. На свежей VM папка
    b1\\Saved\\Config\\Windows\\ ещё не существует (Wukong не запускался) —
    создаём mkdir+parents. КРИТИЧНО подкладывать ДО запуска Wukong: дефолтный
    конфиг UE5 создаётся с PrivacyAgreement=0/FirstSettingFinish=False, и
    тогда тул показывает first-time setup wizard (выбор языка + accept). CDN-
    конфиг от GameServer содержит PrivacyAgreement=1, AgreementReaded=1,
    FirstSettingFinish=True — именно так оригинал обходит wizard."""
    log('Качаю Wukong GameUserSettings.ini: {0}'.format(WUKONG_CONFIG_URL))
    WUKONG_USER_SETTINGS_INI.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(WUKONG_CONFIG_URL, str(WUKONG_USER_SETTINGS_INI))


def delete_old_wukong_results():
    if WUKONG_RESULTS_DIR.exists():
        log('Чищу старые Wukong-результаты: ' + str(WUKONG_RESULTS_DIR))
        shutil.rmtree(str(WUKONG_RESULTS_DIR), ignore_errors=True)


def wait_for_wukong_process(timeout_s):
    log('Жду процесс {0} (timeout {1}s)'.format(WUKONG_PROCESS, timeout_s))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _process_running(WUKONG_PROCESS):
            log('  -> процесс найден')
            return
        time.sleep(2)
    raise TimeoutError('{0} не появился за {1}s'.format(WUKONG_PROCESS, timeout_s))


def wait_for_wukong_result(timeout_s):
    """Любой файл в WUKONG_RESULTS_DIR с size > 0. По CS-коду — берём первый."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if WUKONG_RESULTS_DIR.exists():
            files = [f for f in WUKONG_RESULTS_DIR.iterdir()
                     if f.is_file() and f.stat().st_size > 0]
            if files:
                time.sleep(WUKONG_RESULT_SETTLE_S)
                return files[0]
        time.sleep(WUKONG_RESULT_POLL_S)
    raise TimeoutError('Wukong result не появился за {0}s'.format(timeout_s))


def steam_ensure_logged_in(steam_user, steam_pass, email_creds=None):
    """Если steam.exe запущен, ActiveUser != 0 И Wukong manifest УЖЕ есть —
    skip relogin. Иначе — полный сценарий с kill+manifest+UI-automation.

    Manifest-проверка нужна: если manifest отсутствует, Steam не знает про
    Wukong → applaunch покажет install dialog. Steam подхватит manifest
    ТОЛЬКО при следующем старте — поэтому требуется restart Steam'а
    (через steam_login_with_ui_automation, который kill'ает Steam в начале).

    email_creds: (host, user, pwd) для auto Steam Guard через IMAP, или None
    (тогда 2FA-код вводится вручную через VNC).
    """
    manifest_exists = WUKONG_MANIFEST_PATH.exists()
    if _process_running('steam.exe'):
        active = _read_active_user()
        if active and manifest_exists:
            log('Steam залогинен (ActiveUser={0}) + Wukong manifest на месте — skip relogin'.format(active))
            return active
        if active and not manifest_exists:
            log('Steam залогинен, но Wukong manifest отсутствует — relogin чтобы Steam scan его подхватил')
        elif not active:
            log('Steam запущен, но ActiveUser=0 — относогинимся')
    return steam_login_with_ui_automation(steam_user, steam_pass, email_creds)


def steam_login_with_ui_automation(steam_user, steam_pass, email_creds=None):
    """Воспроизводит BaseSteamClient.LaunchSteamWithAuth → PassAuthorization
    из Common.Steam.dll.

    1. kill_steam + clear registry / loginusers.vdf (форсим показ auth-окна)
    2. start steam.exe
    3. Ждём появления окна class=SDL_app caption="Войти в Steam"
    4. Activate + English keyboard layout
    5. Пишем login через scancode-SendInput, Tab, password, Enter
    6. Поллим ActiveUser в registry до != 0

    raise если что-то не сработало (auth window не появился / ActiveUser не выставился).
    """
    log('Kill Steam + clear login state (force auth window)...')
    kill_steam()
    clear_steam_login_state()

    # Manifest Wukong подкладываем ДО старта Steam — Steam сканит steamapps/
    # ТОЛЬКО при старте. Если положить после Popen — Steam не подхватит и при
    # applaunch покажет install dialog. Это reverse-engineered поведение
    # GameServer'а (он в init_script.add_manifests делает то же самое перед
    # запуском Benchmark.Gta.exe — копирует snapshot из steamapps/manifests/).
    ensure_wukong_manifest()

    # Чистим почту от старых Steam-писем ДО логина — потом UNSEEN search возьмёт
    # только свежее письмо с актуальным кодом, не зацепит устаревшее.
    if email_creds:
        host, user, pwd = email_creds
        log('Pre-cleanup IMAP UNSEEN ({0}@{1})...'.format(user, host))
        mark_all_unseen_as_seen(host, user, pwd)

    log('Запуск {0} с флагами {1}...'.format(STEAM_EXE, STEAM_LAUNCH_FLAGS))
    subprocess.Popen([str(STEAM_EXE)] + STEAM_LAUNCH_FLAGS)
    # Steam обычно 5-15s до auth-окна. Оригинал дал 7s startup + 120s poll.
    time.sleep(7)

    log('Жду auth-окно (timeout {0}s)...'.format(STEAM_AUTH_WAIT_S))
    hwnd = find_steam_auth_window(STEAM_AUTH_WAIT_S)
    if not hwnd:
        # Может, Steam уже залогинился сам (RememberMe + кешированный token)?
        active = _read_active_user()
        if active:
            log('Auth-окно не появилось, но ActiveUser={0} — Steam уже залогинен'.format(active))
            return active
        raise TimeoutError(
            'Steam auth-окно не появилось за {0}s (class={1}). Возможно Steam '
            'не стартанул или caption отличается от {2}.'
            .format(STEAM_AUTH_WAIT_S, STEAM_AUTH_WINDOW_CLASS, STEAM_AUTH_CAPTIONS))

    log('Активирую окно, ставлю английскую раскладку...')
    _activate_window(hwnd)
    _set_english_keyboard(hwnd)
    time.sleep(1)

    log('Печатаю login...')
    _activate_window(hwnd)
    write_line(steam_user)
    time.sleep(0.3)
    log('Tab...')
    _press_scan(_DIK_TAB)
    time.sleep(0.3)
    log('Печатаю password...')
    write_line(steam_pass)
    time.sleep(0.3)
    log('Enter...')
    _press_scan(_DIK_RETURN)

    # Wait 1: после login+pass+Enter Steam либо залогинит сразу (без 2FA),
    # либо покажет Steam Guard prompt в том же окне.
    log('Жду пока ActiveUser в registry станет != 0 (timeout {0}s)...'.format(
        STEAM_ACTIVEUSER_WAIT_S))
    deadline = time.monotonic() + STEAM_ACTIVEUSER_WAIT_S
    while time.monotonic() < deadline:
        active = _read_active_user()
        if active:
            log('  -> залогинено без 2FA, ActiveUser={0}'.format(active))
            return active
        # Если email_creds есть, пробуем fetch код. Steam обычно шлёт письмо за
        # 5-15s после login attempt; делаем несколько попыток.
        if email_creds:
            log('  ActiveUser=0, пытаюсь fetch Steam Guard код из почты...')
            host, user, pwd = email_creds
            code = None
            for attempt in range(1, STEAM_GUARD_ATTEMPTS + 1):
                code = fetch_steam_guard_code(host, user, pwd, steam_user)
                if code:
                    log('  -> код получен на попытке {0}/{1}: {2}'.format(
                        attempt, STEAM_GUARD_ATTEMPTS, code))
                    break
                log('  попытка {0}/{1}: код ещё не пришёл, жду {2}s'.format(
                    attempt, STEAM_GUARD_ATTEMPTS, STEAM_GUARD_INTERVAL_S))
                time.sleep(STEAM_GUARD_INTERVAL_S)
            if not code:
                raise TimeoutError(
                    'Не удалось получить Steam Guard код из почты после {0} попыток. '
                    'Возможно: Steam не отправил письмо / wrong IMAP creds / '
                    'письмо не от Steam Guard.'.format(STEAM_GUARD_ATTEMPTS))
            insert_steam_guard_code(code)
            # После ввода кода даём Steam время на проверку.
            inner_deadline = time.monotonic() + STEAM_GUARD_AFTER_INPUT_WAIT
            while time.monotonic() < inner_deadline:
                active = _read_active_user()
                if active:
                    log('  -> залогинено с 2FA, ActiveUser={0}'.format(active))
                    return active
                time.sleep(2)
            raise TimeoutError(
                'Steam Guard код введён, но ActiveUser остался 0 за {0}s. '
                'Возможно неверный код или Steam отверг (попробуй заново).'
                .format(STEAM_GUARD_AFTER_INPUT_WAIT))
        # Без email_creds — просто ждём, оператор вручную через VNC вводит код.
        time.sleep(2)
    raise TimeoutError(
        'ActiveUser остался 0 после {0}s — Steam не залогинился. Возможно: '
        'wrong credentials / 2FA prompt не закрылся / Steam показал captcha. '
        'Глянь VNC.'.format(STEAM_ACTIVEUSER_WAIT_S))


def run_wukong_benchmark(steam_user, steam_pass, email_creds=None):
    """Возвращает dict с результатом или raise. В обычном режиме не валит
    общий бенч (catch в main); в only_wukong режиме — фатал.
    email_creds=(host, user, pwd): опциональный авто-ввод Steam Guard кода."""
    log('=== Wukong benchmark ===')

    if not STEAM_EXE.exists():
        raise FileNotFoundError('Steam.exe не найден: ' + str(STEAM_EXE))

    download_wukong_config()
    delete_old_wukong_results()

    steam_ensure_logged_in(steam_user, steam_pass, email_creds)

    # Manifest уже подложен в steam_login_with_ui_automation() ДО старта Steam,
    # либо был сохранён Steam'ом после ручной установки. Здесь больше ничего
    # не делаем — applaunch должен просто запустить exe.

    started = False
    for attempt in (1, 2):
        log('applaunch {0} (попытка {1}/2)'.format(WUKONG_STEAM_APPID, attempt))
        subprocess.Popen([str(STEAM_EXE)] + STEAM_LAUNCH_FLAGS +
                         ['-applaunch', str(WUKONG_STEAM_APPID), '-benchmark'])
        wait_total = 0
        max_wait = 5 * 60 if attempt == 1 else WUKONG_APPLAUNCH_WAIT_S
        while wait_total < max_wait:
            time.sleep(5)
            wait_total += 5
            if _process_running(WUKONG_PROCESS):
                log('  -> {0} стартовал за {1}s'.format(WUKONG_PROCESS, wait_total))
                started = True
                break
        if started:
            break
        log('  не стартовал за {0}s, попробую ещё раз'.format(wait_total))
    if not started:
        raise TimeoutError(
            '{0} не появился после 2 applaunch попыток. Возможные причины:\n'
            '  1. На VM не установлен Wukong Benchmark Tool — подключись через '
            'VNC, в Steam найди "Black Myth: Wukong Benchmark Tool" в библиотеке '
            'и нажми "Установить" один раз (этот тул free и быстрый). После — '
            'на этой VM applaunch будет работать автоматом.\n'
            '  2. Тул не активирован на bench-аккаунте — открой '
            'https://store.steampowered.com/app/{1}/ и нажми "Установить".'
            .format(WUKONG_PROCESS, WUKONG_STEAM_APPID))

    # Wizard не появляется: GameUserSettings.ini c CDN уже подложен в
    # download_wukong_config() с PrivacyAgreement=1/FirstSettingFinish=True.

    log('Жду результат в ' + str(WUKONG_RESULTS_DIR))
    result_path = wait_for_wukong_result(WUKONG_BENCH_TIMEOUT_S)
    log('Найден: ' + str(result_path))

    raw = json.loads(result_path.read_text(encoding='utf-8'))
    result = {
        'FPSAvg':      raw.get('FPSAvg'),
        'FPS95':       raw.get('FPS95'),
        'GameVer':     raw.get('GameVer'),
        'source_file': str(result_path),
    }
    log('Результат Wukong: ' + json.dumps(result, ensure_ascii=False))

    log('Kill Wukong-процессов')
    _kill_process(WUKONG_PROCESS)
    _kill_process(WUKONG_SUBPROCESS)

    return result


# ── main: stdout/stderr → last_run.log через dup2, статус всегда пишется ─────
def _redirect_stdio_to_log():
    """Перенаправить fd 1/2 в last_run.log. Через dup2 — чтобы subprocess'ы
    (tasklist, taskkill, ffmpeg) тоже попадали в лог, а не в чёрную дыру PsExec.
    sys.stdout/sys.stderr тоже переоткрываем — иначе print() буферизуется в
    старый объект."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    sys.stdout = os.fdopen(1, 'w', buffering=1, encoding='utf-8', errors='replace')
    sys.stderr = os.fdopen(2, 'w', buffering=1, encoding='utf-8', errors='replace')


def _safe_unlink(p):
    try:
        p.unlink()
    except Exception:
        pass


def _write_status(status):
    STATUS_FILE.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def main():
    config      = sys.argv[1] if len(sys.argv) > 1 else 'vk'
    steam_user  = sys.argv[2] if len(sys.argv) > 2 else ''
    steam_pass  = sys.argv[3] if len(sys.argv) > 3 else ''
    only_wukong = (len(sys.argv) > 4 and sys.argv[4] == '1')
    imap_host   = sys.argv[5] if len(sys.argv) > 5 else ''
    email_user  = sys.argv[6] if len(sys.argv) > 6 else ''
    email_pass  = sys.argv[7] if len(sys.argv) > 7 else ''
    email_creds = (imap_host, email_user, email_pass) if (imap_host and email_user and email_pass) else None

    # Чистим артефакты прошлого запуска ДО редиректа stdio: появление STATUS_FILE
    # = сигнал хосту что мы закончили, его остатки от прошлого прогона дадут
    # ложный hit.
    for f in (LOG_FILE, RESULT_FILE, STATUS_FILE, FFMPEG_LOG):
        _safe_unlink(f)

    _redirect_stdio_to_log()

    wukong_skipped = not (steam_user and steam_pass)
    log('vm_bench start: config={0} cyberpunk={1} wukong={2}'.format(
        config,
        'off' if only_wukong else 'on',
        'on' if not wukong_skipped else 'off',
    ))

    started = time.time()
    rc = -1
    err = None
    ffmpeg_proc = None
    nvenc_died_early = False
    nvenc_returncode = None
    cyberpunk_result = None
    wukong_result = None
    wukong_error = None

    try:
        if only_wukong and wukong_skipped:
            raise RuntimeError(
                'only_wukong=1 но Steam credentials не заданы — нечего запускать. '
                'Передай STEAM_USER/STEAM_PASS env, либо не ставь ONLY_WUKONG.'
            )

        # NVENC ВСЕГДА — без encoder-нагрузки результат не репрезентативен,
        # production стримит постоянно. Если ffmpeg не стартовал — фейлим всё.
        ffmpeg_proc, nvenc_err = start_nvenc_load()
        if ffmpeg_proc is None:
            raise RuntimeError('NVENC load не стартовал: ' + (nvenc_err or 'unknown'))
        log('NVENC load запущен (pid={0})'.format(ffmpeg_proc.pid))

        if not only_wukong:
            cyberpunk_result = run_cyberpunk_benchmark(config)

            # Проверка: ffmpeg должен быть жив всё это время. Если умер посреди
            # бенча — Cyberpunk померил почти-idle под видом production-load,
            # это надо засчитать как фейл, иначе результат ложно-успешный.
            if ffmpeg_proc.poll() is not None:
                nvenc_died_early = True
                nvenc_returncode = ffmpeg_proc.returncode
                raise RuntimeError(
                    'NVENC ffmpeg умер мид-бенч (rc={0}), результат не репрезентативен — '
                    'см. last_ffmpeg.log'.format(nvenc_returncode)
                )

        # Wukong — после Cyberpunk (если был). В only_wukong режиме фейл фатал
        # (нечего больше отдавать), в обычном — мягкий (Cyberpunk уже собран).
        if not wukong_skipped:
            try:
                wukong_result = run_wukong_benchmark(steam_user, steam_pass, email_creds)
            except Exception:
                wukong_error = traceback.format_exc()
                log('Wukong FAIL:\n' + wukong_error)
                if only_wukong:
                    raise

            # NVENC-чек после Wukong (он тоже долгий, ~5 мин).
            if ffmpeg_proc.poll() is not None:
                nvenc_died_early = True
                nvenc_returncode = ffmpeg_proc.returncode
                raise RuntimeError(
                    'NVENC ffmpeg умер во время Wukong (rc={0}) — см. last_ffmpeg.log'
                    .format(nvenc_returncode)
                )

        rc = 0
        RESULT_FILE.write_text(
            json.dumps({'cyberpunk': cyberpunk_result, 'wukong': wukong_result},
                       ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        log('Результат записан в ' + str(RESULT_FILE))
    except Exception:
        err = traceback.format_exc()
        log('FAIL:\n' + err)
    finally:
        # Дополнительная проверка на случай если raise был выше по другому
        # поводу, а ffmpeg тем временем тоже помер — для статуса важно.
        if not nvenc_died_early and ffmpeg_proc is not None and ffmpeg_proc.poll() is not None:
            nvenc_died_early = True
            nvenc_returncode = ffmpeg_proc.returncode
        stop_nvenc_load(ffmpeg_proc)

        now = time.time()
        status = {
            'exit_code':       rc,
            'config':          config,
            'duration_s':      round(now - started, 1),
            'result_present':  RESULT_FILE.exists(),
            'log_present':     LOG_FILE.exists(),
            'started_at':      time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(started)),
            'finished_at':     time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now)),
            'error':           err,
            'nvenc_source':    'lavfi-testsrc-synthetic',
            'nvenc_fidelity_note': (
                'encoder workload matches production (1080p60 H.264 CBR 25Mbps); '
                'capture stage synthetic — production uses DX swap-chain hook '
                'injection via SharedCapture_x64.dll, not reproducible standalone'
            ),
            'nvenc_died_early':   nvenc_died_early,
            'nvenc_returncode':   nvenc_returncode,
            'cyberpunk_skipped':  only_wukong,
            'cyberpunk_present':  cyberpunk_result is not None,
            'wukong_skipped':     wukong_skipped,
            'wukong_present':     wukong_result is not None,
            'wukong_error':       wukong_error,
        }
        _write_status(status)


if __name__ == '__main__':
    main()
