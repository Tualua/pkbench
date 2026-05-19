#!/usr/bin/env python3
"""
pkbench.py — хост-сторонний CLI для бенчмарка на Windows-VM через QEMU GA.

Использование:
    pkbench.py <vm> [config]          — выкатить + VNC + бенч + pull одним заходом
        config: vk (default, 120fps test) | rt (RayTracing) | 2k
        NVENC-нагрузка (ffmpeg 25Mbps CBR / h264_nvenc) запускается ВСЕГДА —
        если ffmpeg не стартовал ИЛИ умер посреди бенча, exit_code != 0.
    pkbench.py cat <vm> <winpath>     — debug: cat файла на VM в stdout

Что делает основной флоу (run_all):
    1. Генерирует свежий пароль gamer, применяет на VM (net user), печатает
    2. Очищает C:\\benchmark\\ на VM (rmdir /s /q + mkdir)
    3. iptables flush на хосте (если root) — для пробивки VNC
    4. Качает ffmpeg/PsExec в локальный кэш, выкатывает в VM
       (мелочь через GA-base64, бинари через временный HTTP-сервер)
    5. Открывает Firewall TCP 5900, активирует gamer + Administrators, стартует vncserver
    6. Печатает VNC-баннер (vm-ip:5900, user, pass)
    7. Запускает бенч через PsExec → cmd.bat → launcher.bat → vm_bench.py
    8. Поллит last_status.json, читает last_result.json (в stdout)
    9. Тянет raw summary.json в CWD как summary_<vm>_<host>_<config>_<dt>.json

Транспорт: libvirt-python (`domain.qemuAgentCommand`). Один UNIX-сокет
подключения к /var/run/libvirt/libvirt-sock на время CLI-команды, никаких
fork-per-call. На OL7 ставится: `yum install libvirt-python`.

Целевой Python: 3.6 (OL7-хост). Никаких 3.7+ фич: нет capture_output=True,
нет f"...{x=}...", нет dataclasses, нет Path.unlink(missing_ok=True).
"""

import base64
import http.server
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

try:
    import libvirt
    # libvirt_qemu — отдельный submodule из того же rpm/pip-пакета, ИМЕННО
    # ЗДЕСЬ живёт qemuAgentCommand. Это функция (не метод domain): её первый
    # аргумент — virDomain. В некоторых старых версиях libvirt-python был
    # одноимённый метод на domain, поэтому путаница.
    import libvirt_qemu
except ImportError as _ex:
    sys.stderr.write(
        '[FAIL] Не найден модуль libvirt/libvirt_qemu (libvirt-python).\n'
        '       На OL7: yum install libvirt-python\n'
        '       На pip: pip install libvirt-python (нужны libvirt-devel)\n'
        '       Detail: {0}\n'.format(_ex)
    )
    sys.exit(1)


# libvirt по умолчанию шлёт ошибки в stderr через свой С-handler. Для нас это
# спам: poll-loop file_exists(last_status.json) каждые 10s, пока бенч идёт
# 6+ минут, генерит 30+ строк "guest-file-open: failed: cannot find file" —
# нормальная ситуация (файла нет пока), не ошибка флоу. Все настоящие ошибки
# приходят как libvirt.libvirtError exception'ы и ловятся в _agent().
# Регистрируем no-op error handler один раз на process.
def _silence_libvirt_errors(_ctx, _err):
    pass


libvirt.registerErrorHandler(_silence_libvirt_errors, None)

# ── Логирование (всё в stderr, stdout оставляем чистым под JSON-результаты) ──
# Цвета только если stderr — TTY (под pipe/cron не плюёмся ANSI-кашей).
_USE_COLOR = sys.stderr.isatty()
_RED    = '\033[0;31m' if _USE_COLOR else ''
_GREEN  = '\033[0;32m' if _USE_COLOR else ''
_YELLOW = '\033[1;33m' if _USE_COLOR else ''
_CYAN   = '\033[0;36m' if _USE_COLOR else ''
_NC     = '\033[0m'    if _USE_COLOR else ''


def info(msg): sys.stderr.write('{0}[INFO]{1} {2}\n'.format(_CYAN,   _NC, msg)); sys.stderr.flush()
def ok(msg):   sys.stderr.write('{0}[ OK ]{1} {2}\n'.format(_GREEN,  _NC, msg)); sys.stderr.flush()
def warn(msg): sys.stderr.write('{0}[WARN]{1} {2}\n'.format(_YELLOW, _NC, msg)); sys.stderr.flush()


def die(msg, rc=1):
    sys.stderr.write('{0}[FAIL]{1} {2}\n'.format(_RED, _NC, msg))
    sys.stderr.flush()
    sys.exit(rc)


_CLIXML_NS    = 'http://schemas.microsoft.com/powershell/2004/04'
_CLIXML_ESC   = re.compile(r'_x([0-9A-Fa-f]{4})_')
_CLIXML_MARK  = '#< CLIXML'


def _decode_clixml(text):
    """PowerShell child-process сериализует stderr через CLIXML — XML с
    <S S="Error">/...</S> блоками + escape-последовательностями _x00NN_ для
    управляющих символов. Это не выключается флагами CLI. Распаковываем:
    извлекаем содержимое <S>-нодов, разворачиваем _xNNNN_ → chr(NN).

    Не-CLIXML строки возвращаются как есть.
    """
    if not text or _CLIXML_MARK not in text:
        return text
    m = re.search(r'<Objs\b.*?</Objs>', text, re.DOTALL)
    if not m:
        return text
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError:
        return text
    out_lines = []
    for s in root.iter('{%s}S' % _CLIXML_NS):
        kind = s.attrib.get('S', '')
        if kind in ('progress', ''):
            continue   # Progress-сообщения PS — мусор, скрываем
        body = s.text or ''
        body = _CLIXML_ESC.sub(lambda mm: chr(int(mm.group(1), 16)), body)
        out_lines.append(body)
    if not out_lines:
        return ''   # был только Progress — ничего полезного
    return ''.join(out_lines).rstrip()


# ════════════════════════════════════════════════════════════════════════════
#  GA: тонкая обёртка над libvirt.virDomain.qemuAgentCommand()
# ════════════════════════════════════════════════════════════════════════════
# libvirt-python timeout-флаги для qemuAgentCommand. Цифровые значения берём не
# из enum (libvirt-python в OL7 может не иметь VIR_DOMAIN_QEMU_AGENT_COMMAND_*
# атрибутов на старых версиях), а напрямую: BLOCK=-1, DEFAULT=-2, NOWAIT=-3,
# SHUTDOWN=-4 (см. include/libvirt/libvirt-qemu.h). DEFAULT использует встроенный
# таймаут libvirt'а (~5с), что нам мало для read'ов больших файлов — ставим явный
# secs.
_AGENT_TIMEOUT_SHORT = 10    # ping/file-open/close/exec
_AGENT_TIMEOUT_LONG  = 60    # file-read/write big chunks


class GA(object):
    """Не кидает исключений на failed GA-call — возвращает None, чтобы вызывающий
    мог отличить "файла нет" от "VM упала", без try/except вокруг каждой проверки.
    Критично для poll'ов вида ga.file_exists(...).

    libvirt connection держим один на CLI-команду; libvirt.open() — это unix-socket
    к /var/run/libvirt/libvirt-sock, не subprocess. Закроется при выходе процесса.
    """

    def __init__(self, vm):
        self.vm = vm
        try:
            self._conn = libvirt.open('qemu:///system')
        except libvirt.libvirtError as ex:
            die('Не подключился к libvirt (qemu:///system): {0}\n'
                '       Проверь: systemctl status libvirtd; группа libvirt у пользователя.'.format(ex))
        try:
            self._dom = self._conn.lookupByName(vm)
        except libvirt.libvirtError as ex:
            die('Не нашёл домен "{0}" в libvirt: {1}'.format(vm, ex))

    def _agent(self, payload, timeout=_AGENT_TIMEOUT_SHORT):
        """Отправить JSON в QGA. Возвращает распарсенный response dict или None
        при любой ошибке (libvirt error, таймаут, не-JSON, нет 'return').

        libvirt_qemu.qemuAgentCommand(domain, cmd_str, timeout_seconds, flags=0).
        Положительный timeout — секунды; отрицательные — спец-значения, см.
        константы выше.
        """
        try:
            resp = libvirt_qemu.qemuAgentCommand(self._dom, json.dumps(payload),
                                                 timeout, 0)
        except libvirt.libvirtError:
            return None
        if not resp:
            return None
        try:
            return json.loads(resp)
        except Exception:
            return None

    # ── основное ────────────────────────────────────────────────────────────
    def ping(self):
        r = self._agent({'execute': 'guest-ping'})
        return r is not None and 'return' in r

    def exec(self, path, args=None, capture=False):
        """Запустить программу. Возвращает pid или None."""
        payload = {
            'execute': 'guest-exec',
            'arguments': {'path': path, 'arg': list(args or [])},
        }
        if capture:
            payload['arguments']['capture-output'] = True
        r = self._agent(payload)
        if r is None:
            return None
        return r.get('return', {}).get('pid')

    def exec_status(self, pid):
        r = self._agent({'execute': 'guest-exec-status', 'arguments': {'pid': pid}})
        return None if r is None else r.get('return')

    def exec_wait(self, path, args=None, timeout=30, capture=True):
        """Запустить и дождаться завершения. Возвращает (rc, stdout, stderr).
        rc = None при таймауте / ошибке запуска."""
        pid = self.exec(path, args, capture=capture)
        if pid is None:
            return (None, '', '')
        deadline = time.monotonic() + timeout
        st = None
        while time.monotonic() < deadline:
            st = self.exec_status(pid)
            if st and st.get('exited'):
                break
            time.sleep(1)
        if not (st and st.get('exited')):
            return (None, '', '')
        rc = st.get('exitcode', 0)
        out = base64.b64decode(st.get('out-data', '') or '').decode('utf-8', errors='replace')
        err = base64.b64decode(st.get('err-data', '') or '').decode('utf-8', errors='replace')
        return (rc, out, err)

    def ps_wait(self, script, timeout=30):
        """Выполнить PowerShell-скрипт. Передача через -EncodedCommand
        (UTF-16LE/base64) — обходит quoting hell.

        Обёртка: ErrorActionPreference=Stop + try/catch шлёт error message в
        stdout как plain text (`ERROR: ...`). Иначе ошибки PS-cmdlet'ов уходят
        в stderr через CLIXML, причём часто в `<Obj S="ErrorRecord">` node'е,
        который простой `<S S="Error">`-парсер пропустит. С нашей обёрткой
        большинство runtime-ошибок ловятся читаемо.

        Per-cmdlet `-ErrorAction SilentlyContinue` продолжает работать
        (overrides preference, не глобальный Stop)."""
        wrapped = (
            "$ErrorActionPreference = 'Stop'\n"
            "$ProgressPreference = 'SilentlyContinue'\n"
            "try {\n"
            + script + "\n"
            "} catch {\n"
            "    Write-Output ('ERROR: ' + $_.Exception.Message)\n"
            "    Write-Output ('  at: ' + $_.InvocationInfo.PositionMessage)\n"
            "    exit 1\n"
            "}\n"
        )
        b64 = base64.b64encode(wrapped.encode('utf-16-le')).decode('ascii')
        rc, out, err = self.exec_wait(
            'powershell.exe',
            ['-NoProfile', '-NonInteractive',
             '-InputFormat', 'None', '-OutputFormat', 'Text',
             '-EncodedCommand', b64],
            timeout=timeout,
        )
        return rc, _decode_clixml(out), _decode_clixml(err)

    # ── файлы ───────────────────────────────────────────────────────────────
    def file_exists(self, path):
        r = self._agent({'execute': 'guest-file-open',
                         'arguments': {'path': path, 'mode': 'r'}})
        if r is None or not isinstance(r.get('return'), int):
            return False
        self._agent({'execute': 'guest-file-close',
                     'arguments': {'handle': r['return']}})
        return True

    def file_read(self, path, chunk=256 * 1024):
        """Прочитать файл целиком. Возвращает bytes или None."""
        r = self._agent({'execute': 'guest-file-open',
                         'arguments': {'path': path, 'mode': 'rb'}})
        if r is None or not isinstance(r.get('return'), int):
            return None
        handle = r['return']
        buf = bytearray()
        try:
            while True:
                fr = self._agent({'execute': 'guest-file-read',
                                  'arguments': {'handle': handle, 'count': chunk}},
                                 timeout=_AGENT_TIMEOUT_LONG)
                if fr is None:
                    break
                ret = fr.get('return', {})
                b64 = ret.get('buf-b64', '')
                if b64:
                    buf.extend(base64.b64decode(b64))
                if ret.get('eof'):
                    break
                if not b64:
                    break
        finally:
            self._agent({'execute': 'guest-file-close', 'arguments': {'handle': handle}})
        return bytes(buf)

    def file_write(self, path, data, chunk=3 * 1024 * 1024):
        """Записать bytes в файл на VM. Возвращает True/False."""
        if isinstance(data, str):
            data = data.encode('utf-8')
        r = self._agent({'execute': 'guest-file-open',
                         'arguments': {'path': path, 'mode': 'wb'}})
        if r is None or not isinstance(r.get('return'), int):
            return False
        handle = r['return']
        b64 = base64.b64encode(data).decode('ascii')
        offset = 0
        try:
            while offset < len(b64):
                self._agent({'execute': 'guest-file-write',
                             'arguments': {'handle': handle,
                                           'buf-b64': b64[offset:offset + chunk]}},
                            timeout=_AGENT_TIMEOUT_LONG)
                offset += chunk
            return True
        finally:
            self._agent({'execute': 'guest-file-close', 'arguments': {'handle': handle}})

    def delete(self, path):
        # PowerShell с одиночными кавычками — литерал, no quoting issues.
        # -ErrorAction SilentlyContinue: если файла нет — молча ок.
        self.ps_wait(
            "Remove-Item -LiteralPath '{0}' -Force -ErrorAction SilentlyContinue"
            .format(path), timeout=10)

    def mkdir(self, path):
        # ВАЖНО: через `cmd /c "if not exist X md X"` (как было раньше) ломалось
        # квотингом — cmd видел литеральные кавычки в командной строке после
        # передачи arg-vector через guest-exec, и md не выполнялся. PS-вариант
        # с одиночными кавычками литерален, -Force = не падать если уже есть.
        rc, _, err = self.ps_wait(
            "New-Item -ItemType Directory -Force -Path '{0}' | Out-Null"
            .format(path), timeout=10)
        if rc != 0:
            sys.stderr.write('[WARN] mkdir "{0}" rc={1}: {2}\n'.format(path, rc, err))
        return rc == 0

    def rmdir_recursive(self, path):
        """Рекурсивная очистка папки. Тихо, без падения если папки нет."""
        self.ps_wait(
            "Remove-Item -LiteralPath '{0}' -Recurse -Force -ErrorAction SilentlyContinue"
            .format(path), timeout=30)

    def get_ipv4(self):
        """Первый non-loopback IPv4 интерфейса VM (для построения URL ↔ хост)."""
        r = self._agent({'execute': 'guest-network-get-interfaces'})
        if r is None:
            return None
        for iface in r.get('return', []):
            if 'loopback' in iface.get('name', '').lower():
                continue
            for ip in iface.get('ip-addresses', []) or []:
                if ip.get('ip-address-type') == 'ipv4':
                    return ip.get('ip-address')
        return None


# ════════════════════════════════════════════════════════════════════════════
#  Шаблоны .bat (генерируются при deploy, на диске только тут)
# ════════════════════════════════════════════════════════════════════════════
# bench_psexec.bat: запускает run_via_ga_launcher.bat через PsExec под gamer'ом.
# Ключевые странности (документированы в комментариях .bat'а — оставляю в виде
# heredoc'а, т.к. .bat создаётся при каждом deploy):
#   • -i 1 (явно session 1, без номера PsExec ищет console session через
#     WTSGetActiveConsoleSessionId — на headless VM ненадёжно)
#   • PsExec → cmd.exe → launcher.bat → python.exe, а НЕ PsExec → python.exe
#     напрямую (CreateProcessAsUser для python.exe возвращает ERROR_LOGON_FAILURE,
#     Windows API quirk).
#   • ОДНА строка-аргумент после /c (не два quoted), чтобы cmd MSDN-правилом 2
#     не строил мусор из 4 кавычек.
# Имя scheduled-task'а для запуска vm_bench под gamer'ом.
# Раньше тут были BENCH_PSEXEC_BAT + RUN_VIA_GA_LAUNCHER_BAT — генерируемые
# .bat обёртки для запуска через PsExec → cmd → launcher → python. Заменено
# на Windows Task Scheduler (schtasks) — никакого cmd, никаких .bat, никакого
# PsExec. Task создаётся через PowerShell Register-ScheduledTask с Logon
# Type=InteractiveOrPassword + RunLevel Highest, запускается через Start-
# ScheduledTask, удаляется в finally.
SCHTASK_NAME = 'pkbench_run'


# ════════════════════════════════════════════════════════════════════════════
#  Пути и константы
# ════════════════════════════════════════════════════════════════════════════
SCRIPT_DIR = Path(__file__).resolve().parent
VM_BENCH_PY_LOCAL = SCRIPT_DIR / 'vm_bench.py'
# Кеш бинарей рядом с pkbench.py — на хосте это /root/benchmark/vm_deploy_cache/
# (после sync_to_host файлы лежат плоско в /root/benchmark/), локально в
# devcontainer'е — /workspaces/pkbench/benchmark/vm_deploy_cache/. ~200MB,
# не для git.
CACHE_DIR = SCRIPT_DIR / 'vm_deploy_cache'

# Файлы VM
VM_BENCH_DIR = r'C:\benchmark'
VM_PYTHON    = r'C:\Program Files (x86)\Python36-32\python.exe'
# PsExec.exe больше не используется (заменено на schtasks). Если оставлять — не
# мешает, на VM просто лежит без вызовов. Не деплоим.
VM_FFMPEG    = r'C:\benchmark\ffmpeg.exe'
VM_VMBENCH   = r'C:\benchmark\vm_bench.py'
VM_PYTHONW   = r'C:\Program Files (x86)\Python36-32\pythonw.exe'
VM_STATUS_JSON   = r'C:\benchmark\last_status.json'
VM_RESULT_JSON   = r'C:\benchmark\last_result.json'
VM_RUN_LOG       = r'C:\benchmark\last_run.log'

# URLs для скачивания бинарей
FFMPEG_URL  = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
PSTOOLS_URL = 'https://download.sysinternals.com/files/PSTools.zip'

def _generate_gamer_pass():
    """Свежий случайный пароль на каждый прогон. На диск не пишется — printится
    в VNC-баннере для оператора, в коде используется напрямую (PsExec, net user).
    Это значит каждый запуск pkbench убивает текущую VNC-сессию: ОК, потому
    что один запуск = один прогон, между ними подключаться особо некуда."""
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(24))


def _resolve_email_credentials():
    """IMAP-credentials для авто-ввода Steam Guard кода. Опционально.

    Приоритет: env STEAM_GUARD_IMAP_HOST / STEAM_GUARD_EMAIL / STEAM_GUARD_EMAIL_PASS
    → интерактив (если tty и хоть одно поле задано) → пусто (manual 2FA через VNC).

    Возвращает (host, user, pwd). Если что-то пустое — Wukong поток будет
    ждать ручного ввода кода через VNC до timeout.
    """
    host = os.environ.get('STEAM_GUARD_IMAP_HOST', '').strip()
    user = os.environ.get('STEAM_GUARD_EMAIL', '').strip()
    pwd  = os.environ.get('STEAM_GUARD_EMAIL_PASS', '')
    if host and user and pwd:
        return host, user, pwd
    if not sys.stdin.isatty():
        return host, user, pwd
    if not (host or user or pwd):
        # Ничего не задано — не спрашиваем, оператор сам решит ручной или авто.
        return '', '', ''

    import getpass
    sys.stderr.write(
        '\nSteam Guard auto-input: задано часть IMAP-creds, доспрошу остальное.\n'
        '(Полный путь через env: STEAM_GUARD_IMAP_HOST=imap.example.com '
        'STEAM_GUARD_EMAIL=... STEAM_GUARD_EMAIL_PASS=...)\n'
    )
    if not host:
        try:
            host = input('IMAP host (Enter — пропустить auto Steam Guard): ').strip()
        except EOFError:
            return '', '', ''
    if not host:
        return '', '', ''
    if not user:
        try:
            user = input('Email login: ').strip()
        except EOFError:
            return '', '', ''
    if not user:
        return '', '', ''
    if not pwd:
        try:
            pwd = getpass.getpass('Email password (для Gmail — App Password): ')
        except EOFError:
            return '', '', ''
    return host, user, pwd


def _resolve_steam_credentials():
    """Steam credentials для опционального Wukong-таска.

    Приоритет: env STEAM_USER/STEAM_PASS → интерактивный prompt (если tty) →
    пусто (Wukong пропускается).

    Возвращает (user, pass). Если оба пустые — Wukong skip.
    """
    user = os.environ.get('STEAM_USER', '').strip()
    pwd  = os.environ.get('STEAM_PASS', '')
    if user and pwd:
        return user, pwd

    if not sys.stdin.isatty():
        # cron/non-interactive — не блокируемся на input(), просто skip.
        return user, pwd

    import getpass
    sys.stderr.write(
        '\nWukong (опционально): нужен Steam-аккаунт БЕЗ 2FA.\n'
        'Можно задать env: STEAM_USER=... STEAM_PASS=... ./pkbench.py ...\n'
    )
    if not user:
        try:
            user = input('Steam login (Enter — пропустить Wukong): ').strip()
        except EOFError:
            return '', ''
    if not user:
        return '', ''
    if not pwd:
        try:
            pwd = getpass.getpass('Steam password: ')
        except EOFError:
            return '', ''
    return user, pwd


# ════════════════════════════════════════════════════════════════════════════
#  HTTP-fast-transfer (большие бинари: ffmpeg ~200MB, PsExec ~500KB)
# ════════════════════════════════════════════════════════════════════════════
def _host_ip_for_vm(ga, vm):
    """IP-хоста, который VM увидит как источник. Получаем IPv4 VM через GA,
    дальше `ip route get <vm-ip>` — ядро вернёт src-IP. Симметрично работает
    для bridge/NAT/direct/macvtap, независимо от имени интерфейса хоста."""
    vm_ip = ga.get_ipv4()
    if not vm_ip:
        die('Не получил IPv4 от GA — VM не загружена или нет адреса')
    r = subprocess.run(['ip', '-4', 'route', 'get', vm_ip],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        die('Нет маршрута к {0} (VM в изолированной сети?)'.format(vm_ip))
    # формат: "<vm_ip> via <gw> dev <iface> src <host_ip> uid 0"
    tokens = r.stdout.decode().split()
    for i, t in enumerate(tokens):
        if t == 'src' and i + 1 < len(tokens):
            return vm_ip, tokens[i + 1]
    die('Не нашёл src-IP в `ip route get {0}`'.format(vm_ip))


class _BenchHTTPState(object):
    """Shared state HTTP-сервера. Один сервер живёт всё время run_all и
    обслуживает И GET (deploy ffmpeg/PsExec в VM), И POST (live-логи + push
    артефактов от vm_bench.py)."""
    serve_dir = ''       # GET serve root (CACHE_DIR при deploy)
    token = ''           # Authorization Bearer для POST
    vm = ''
    host_short = ''
    timestamp = ''       # для имён сохранённых артефактов на хосте
    done_event = None    # threading.Event, выставляется на POST /done
    received_status = None
    received_artifacts = None   # dict: original_name -> local Path
    out_lock = None      # threading.Lock для атомарного stderr.write


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """ThreadingHTTPServer в stdlib только с 3.7. У нас 3.6 — собираем сами."""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # PowerShell Invoke-WebRequest закрывает keep-alive коннект после GET,
        # наш HTTP/1.1 сервер пытается читать следующий request → ECONNRESET.
        # Это нормально, не шумим трейсбэками.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class _BenchHTTPHandler(http.server.BaseHTTPRequestHandler):
    """GET /<path>           → serve файла из state.serve_dir (deploy)
       POST /log             → batched vm_bench log-lines в stderr с префиксом
       POST /ffmpeg          → batched ffmpeg stdout/stderr в stderr с префиксом
       POST /artifact?name=N → save body в CWD как <vm>_<host>_<dt>_N
       POST /done            → body = status JSON, выставляет done_event

       Все POST требуют header Authorization: Bearer <token>. GET — без auth
       (deploy через Invoke-WebRequest без header).
    """
    server_version = 'pkbench/1.0'
    protocol_version = 'HTTP/1.1'   # keep-alive helps live-логам

    def log_message(self, fmt, *args):
        # Подавляем дефолтный per-request stderr-лог HTTP-сервера —
        # иначе каждый POST /log даст две строки шума.
        return

    @property
    def _state(self):
        return self.server._state

    def _check_auth(self):
        expected = 'Bearer ' + self._state.token
        if self.headers.get('Authorization', '') != expected:
            self.send_response(403)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return False
        return True

    def _send_ok(self, code=200):
        self.send_response(code)
        self.send_header('Content-Length', '0')
        self.end_headers()

    # ── GET (deploy serve) ──────────────────────────────────────────────────
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        parts = [p for p in urllib.parse.unquote(path).split('/') if p and p != '.']
        if '..' in parts:
            self.send_response(400)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        full = os.path.join(self._state.serve_dir, *parts)
        if not os.path.isfile(full):
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        size = os.path.getsize(full)
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        self.end_headers()
        with open(full, 'rb') as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # ── POST (live monitoring + artifacts) ──────────────────────────────────
    def do_POST(self):
        if not self._check_auth():
            return
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get('Content-Length', '0') or '0')
        body = self.rfile.read(length) if length else b''
        try:
            if path == '/log':
                self._handle_log(body, '[vm  ]')
            elif path == '/ffmpeg':
                self._handle_log(body, '[ffmp]')
            elif path == '/artifact':
                self._handle_artifact(body)
            elif path == '/done':
                self._handle_done(body)
            else:
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
        except Exception as ex:
            with self._state.out_lock:
                warn('HTTP handler error on {0}: {1}'.format(path, ex))
            self.send_response(500)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self._send_ok()

    def _handle_log(self, body, prefix):
        text = body.decode('utf-8', errors='replace')
        if not text:
            return
        st = self._state
        with st.out_lock:
            for line in text.splitlines():
                sys.stderr.write('{0} {1}: {2}\n'.format(prefix, st.vm, line))
            sys.stderr.flush()

    def _handle_artifact(self, body):
        st = self._state
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        name = (params.get('name') or [''])[0]
        # Безопасность: имя — только basename, без слешей.
        if not name or '/' in name or '\\' in name or '..' in name:
            raise ValueError('invalid artifact name: ' + name)
        fname = '{vm}_{host}_{dt}_{name}'.format(
            vm=st.vm, host=st.host_short, dt=st.timestamp, name=name)
        local = Path.cwd() / fname
        local.write_bytes(body)
        st.received_artifacts[name] = local
        with st.out_lock:
            ok('artifact <- {0} ({1} bytes) saved as {2}'.format(
                name, len(body), local.name))

    def _handle_done(self, body):
        st = self._state
        try:
            st.received_status = json.loads(body.decode('utf-8', errors='replace'))
        except Exception:
            st.received_status = {'exit_code': -1, 'error': 'invalid /done body'}
        with st.out_lock:
            ok('/done received')
        st.done_event.set()


def _start_http_server(serve_dir, vm, host_short, timestamp, port):
    """Поднять threaded HTTP-сервер для всего run_all (GET + POST endpoints).
    Возвращает (server, state). Остановка — server.shutdown()."""
    # ss-style проверка порта (быстрее, чем bind→fail с stack trace).
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('0.0.0.0', port))
        s.close()
    except OSError:
        die('Порт {0} уже занят на хосте (используй HTTP_PORT=NNNN)'.format(port))

    state = _BenchHTTPState()
    state.serve_dir = str(serve_dir)
    import secrets as _secrets
    state.token = _secrets.token_hex(16)
    state.vm = vm
    state.host_short = host_short
    state.timestamp = timestamp
    state.done_event = threading.Event()
    state.received_status = None
    state.received_artifacts = {}
    state.out_lock = threading.Lock()

    server = _ThreadingHTTPServer(('0.0.0.0', port), _BenchHTTPHandler)
    server._state = state

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, state


def _http_put_via_vm(ga, host_ip, port, filename, dst_win_path,
                     label=None, timeout_s=600):
    """Дёрнуть Invoke-WebRequest на VM, чтобы стянула файл с host:port/filename."""
    label = label or filename
    ps_script = (
        "$ProgressPreference = 'SilentlyContinue'\n"
        "try {{\n"
        "    Invoke-WebRequest -Uri 'http://{ip}:{port}/{fname}' "
        "-OutFile '{dst}' -UseBasicParsing\n"
        "    exit 0\n"
        "}} catch {{\n"
        "    Write-Error $_.Exception.Message\n"
        "    exit 1\n"
        "}}"
    ).format(ip=host_ip, port=port, fname=filename, dst=dst_win_path)

    sys.stderr.write('  {0:45s} ... '.format(label))
    sys.stderr.flush()
    rc, _, err = ga.ps_wait(ps_script, timeout=timeout_s)
    if rc == 0:
        sys.stderr.write('OK\n')
        return True
    sys.stderr.write('FAIL (rc={0})\n'.format(rc))
    if err:
        warn('PowerShell stderr:\n' + err)
    return False


# ════════════════════════════════════════════════════════════════════════════
#  Утилиты деплоя (скачивание бинарей, заливка через GA)
# ════════════════════════════════════════════════════════════════════════════
def _download_ffmpeg(dst_exe):
    """Скачивает ffmpeg.exe в dst_exe. Кэширует."""
    if dst_exe.exists():
        ok('ffmpeg.exe уже скачан ({0})'.format(_human_size(dst_exe.stat().st_size)))
        return
    info('Качаю ffmpeg (BtbN win64-gpl)...')
    zip_path = dst_exe.with_suffix('.zip')
    urllib.request.urlretrieve(FFMPEG_URL, str(zip_path))
    info('Распаковываю ffmpeg.exe из zip...')
    with zipfile.ZipFile(str(zip_path)) as zf:
        # В архиве: ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe
        members = [n for n in zf.namelist() if n.endswith('/bin/ffmpeg.exe')]
        if not members:
            die('В архиве не нашёл */bin/ffmpeg.exe')
        with zf.open(members[0]) as src, open(str(dst_exe), 'wb') as dst:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                dst.write(buf)
    zip_path.unlink()
    ok('ffmpeg.exe готов ({0})'.format(_human_size(dst_exe.stat().st_size)))


def _download_psexec(dst_exe):
    if dst_exe.exists():
        ok('PsExec.exe уже скачан ({0})'.format(_human_size(dst_exe.stat().st_size)))
        return
    info('Качаю PSTools.zip...')
    zip_path = dst_exe.parent / 'PSTools.zip'
    urllib.request.urlretrieve(PSTOOLS_URL, str(zip_path))
    info('Извлекаю PsExec.exe...')
    with zipfile.ZipFile(str(zip_path)) as zf:
        with zf.open('PsExec.exe') as src, open(str(dst_exe), 'wb') as dst:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                dst.write(buf)
    zip_path.unlink()
    ok('PsExec.exe готов ({0})'.format(_human_size(dst_exe.stat().st_size)))


def _human_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return '{0:.1f} {1}'.format(n, unit)
        n /= 1024.0
    return '{0:.1f} TB'.format(n)


def _ga_put(ga, src_path, dst_win, label=None):
    """Файл локально → VM через GA-base64. Для мелочи (.py, .bat)."""
    label = label or src_path.name
    size = src_path.stat().st_size
    sys.stderr.write('  {0:45s} {1:>10s} ... '.format(label, _human_size(size)))
    sys.stderr.flush()
    data = src_path.read_bytes()
    if ga.file_write(dst_win, data):
        sys.stderr.write('OK\n')
        return True
    sys.stderr.write('FAIL\n')
    return False


def _ga_put_text(ga, text, dst_win, label):
    """Сгенерированный текст (.bat) → VM через GA."""
    data = text.encode('utf-8')
    sys.stderr.write('  {0:45s} {1:>10s} ... '.format(label, _human_size(len(data))))
    sys.stderr.flush()
    if ga.file_write(dst_win, data):
        sys.stderr.write('OK\n')
        return True
    sys.stderr.write('FAIL\n')
    return False


# ════════════════════════════════════════════════════════════════════════════
#  Основной флоу
# ════════════════════════════════════════════════════════════════════════════
POLL_INTERVAL_S = 10
POLL_TIMEOUT_PER_ITER_S = 30 * 60   # 30 мин на одну внешнюю итерацию + запас

# PowerShell-snippet для поиска ПОСЛЕДНЕЙ benchmark_<dt> папки и вывода полного
# пути summary.json. Возвращает путь через stdout, exit-code:
#   0  ок (путь в stdout)
#   2  benchmarkResults не существует
#   3  benchmarkResults пуст
#   4  summary.json нет в последней папке
PS_FIND_LAST_SUMMARY = (
    "$base = 'C:\\Users\\gamer\\Documents\\CD Projekt Red\\Cyberpunk 2077\\benchmarkResults'\n"
    "if (-not (Test-Path -LiteralPath $base)) { exit 2 }\n"
    "$last = Get-ChildItem -LiteralPath $base -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1\n"
    "if (-not $last) { exit 3 }\n"
    "$summary = Join-Path $last.FullName 'summary.json'\n"
    "if (-not (Test-Path -LiteralPath $summary)) { exit 4 }\n"
    "Write-Output $summary\n"
)

# Аналогично для Wukong: один файл в C:\Users\gamer\AppData\Local\Temp\b1\
# BenchMarkHistory\Tool\<filename>. Имя не фиксированное.
PS_FIND_WUKONG_RESULT = (
    "$base = 'C:\\Users\\gamer\\AppData\\Local\\Temp\\b1\\BenchMarkHistory\\Tool'\n"
    "if (-not (Test-Path -LiteralPath $base)) { exit 2 }\n"
    "$f = Get-ChildItem -LiteralPath $base -File | Where-Object { $_.Length -gt 0 } "
    "| Sort-Object LastWriteTime -Descending | Select-Object -First 1\n"
    "if (-not $f) { exit 3 }\n"
    "Write-Output $f.FullName\n"
)


def _iptables_flush_if_root():
    """iptables -F + policy ACCEPT по всем chains/tables. NoOp если не root.
    Нужен для дев-окружения (VNC через хост к гостю)."""
    if os.geteuid() != 0:
        info('iptables flush: пропуск (не root — запусти через sudo если VNC не пробивается)')
        return
    info('iptables flush на хосте (policy ACCEPT)...')
    for table in ('filter', 'nat', 'mangle', 'raw'):
        subprocess.run(['iptables', '-t', table, '-F'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(['iptables', '-t', table, '-X'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for chain in ('INPUT', 'FORWARD', 'OUTPUT'):
        subprocess.run(['iptables', '-P', chain, 'ACCEPT'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for chain in ('PREROUTING', 'POSTROUTING', 'OUTPUT'):
        subprocess.run(['iptables', '-t', 'nat', '-P', chain, 'ACCEPT'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ok('iptables flushed')


def _setup_autologon(ga, user, pw):
    """Прописать HKLM\\...\\Winlogon\\AutoAdminLogon=1 + DefaultUserName/Password,
    чтобы после reboot Windows автоматически залогинил gamer'а без UI.

    Пароль кладётся в registry plaintext'ом (стандартное поведение Windows
    autologon). Для эфемерных bench-VM приемлемо. AutoLogonCount удаляем —
    иначе если он есть и =0, autologon отключится после первой загрузки.
    """
    info('Включаю autologon для {0} (после reboot)...'.format(user))
    # Одинарные кавычки в PowerShell — литерал, не интерпретируется. Пароль
    # передаём через variable assignment чтобы избежать quote-mangling.
    ps_script = (
        "$pw = '{pw}'\n"
        "$k  = 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon'\n"
        "Set-ItemProperty -Path $k -Name AutoAdminLogon  -Value '1'    -Type String\n"
        "Set-ItemProperty -Path $k -Name DefaultUserName -Value '{user}' -Type String\n"
        "Set-ItemProperty -Path $k -Name DefaultDomainName -Value $env:COMPUTERNAME -Type String\n"
        "Set-ItemProperty -Path $k -Name DefaultPassword -Value $pw -Type String\n"
        "Remove-ItemProperty -Path $k -Name AutoLogonCount -ErrorAction SilentlyContinue\n"
    ).format(user=user, pw=pw.replace("'", "''"))
    rc, out, err = ga.ps_wait(ps_script, timeout=15)
    if rc != 0:
        msg = (out or '').strip() or (err or '').strip() or '(no PS output)'
        warn('autologon registry write rc={0}:\n{1}'.format(rc, msg))
    else:
        ok('Autologon настроен (active после reboot)')


def _launch_via_schtasks(ga, py_argv):
    """Создать и запустить scheduled task под gamer для pythonw.exe vm_bench.py.

    Заменяет PsExec + bench_psexec.bat + run_via_ga_launcher.bat одним шагом.
    LogonType=Interactive — task запустится в active session gamer'а (нужен
    autologon после reboot ИЛИ уже-залогиненный gamer). Password не нужен —
    interactive token уже-аутентифицированной сессии.

    py_argv — список args, которые получит vm_bench.py (без exe-пути).
    """
    # Собираем PowerShell-литерал массива аргументов: каждую строку оборачиваем
    # в одинарные кавычки + одинарные кавычки внутри удваиваем. Это даёт чистый
    # литерал PS без интерполяции.
    def _ps_quote(s):
        return "'" + s.replace("'", "''") + "'"

    py_argv_full = [r'C:\benchmark\vm_bench.py'] + list(py_argv)
    ps_args_arr = '@(' + ', '.join(_ps_quote(a) for a in py_argv_full) + ')'
    name_q = _ps_quote(SCHTASK_NAME)
    pyw_q = _ps_quote(VM_PYTHONW)

    # Register-ScheduledTask с -Trigger Once в далёком будущем (триггер не нужен,
    # запускаем сразу через Start-ScheduledTask). InteractiveOrPassword =
    # task запускается в interactive session пользователя.
    # LogonType=Interactive: task запустится в active session gamer'а (нужен
    # autologon после reboot ИЛИ уже залогиненный gamer). Password не нужен —
    # interactive token используется уже-аутентифицированной сессией.
    # ВАЖНО: -LogonType — параметр New-ScheduledTaskPrincipal, НЕ Register-
    # ScheduledTask. Если передать его прямо в Register-ScheduledTask —
    # "parameter cannot be found".
    ps_script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$name = {name}\n"
        "$exe  = {pyw}\n"
        "$args = {args}\n"
        "$argString = ($args | ForEach-Object {{ '\"' + $_ + '\"' }}) -join ' '\n"
        "$action = New-ScheduledTaskAction -Execute $exe -Argument $argString\n"
        "$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddYears(99))\n"
        "$settings = New-ScheduledTaskSettingsSet "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::FromHours(2))\n"
        "$principal = New-ScheduledTaskPrincipal -UserId 'gamer' "
        "-LogonType Interactive -RunLevel Highest\n"
        "Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue\n"
        "Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger "
        "-Settings $settings -Principal $principal -Force | Out-Null\n"
        "Start-ScheduledTask -TaskName $name\n"
        "Write-Output 'task started'\n"
    ).format(name=name_q, pyw=pyw_q, args=ps_args_arr)

    rc, out, err = ga.ps_wait(ps_script, timeout=30)
    if rc != 0:
        die('schtasks register/start failed: rc={0}\nstdout: {1}\nstderr: {2}'.format(
            rc, (out or '').strip(), (err or '').strip()), rc=3)
    ok('Scheduled task "{0}" started under gamer'.format(SCHTASK_NAME))


def _cleanup_schtask(ga):
    """Удалить scheduled task в финале (best-effort, тихо)."""
    ps_script = (
        "Unregister-ScheduledTask -TaskName '{0}' -Confirm:$false "
        "-ErrorAction SilentlyContinue\n"
    ).format(SCHTASK_NAME)
    try:
        ga.ps_wait(ps_script, timeout=10)
    except Exception:
        pass


def _setup_vnc(ga, pw):
    """Активировать gamer + в Administrators, открыть Firewall 5900, стартануть
    RealVNC. Возвращает True если 5900 на VM слушается. Пароль gamer ставится
    отдельно (раньше по флоу) — здесь только активация."""
    info('Активирую gamer + в Administrators...')
    ga.exec_wait(r'C:\Windows\System32\net.exe',
                 ['user', 'gamer', '/active:yes'], timeout=10)
    ga.exec_wait(r'C:\Windows\System32\net.exe',
                 ['localgroup', 'Administrators', 'gamer', '/add'], timeout=10)

    info('Windows Firewall TCP 5900 (VNC)...')
    fw_script = (
        "if (-not (Get-NetFirewallRule -DisplayName 'VNC-pkbench' -ErrorAction SilentlyContinue)) { "
        "New-NetFirewallRule -DisplayName 'VNC-pkbench' -Direction Inbound -Protocol TCP "
        "-LocalPort 5900 -Action Allow -Profile Any | Out-Null "
        "}"
    )
    ga.ps_wait(fw_script, timeout=15)

    info('Старт vncserver service...')
    ga.exec_wait(r'C:\Windows\System32\sc.exe', ['start', 'vncserver'], timeout=10)


def _qga_fallback_pull(ga, state, vm):
    """Если HTTP /done не получен — пробуем достать last_status/result/run/ffmpeg
    через QGA file_read и заполнить state.received_status + received_artifacts.
    Используется при HTTP-timeout как graceful degradation."""
    pairs = [
        (VM_STATUS_JSON,                'last_status.json'),
        (VM_RESULT_JSON,                'last_result.json'),
        (VM_RUN_LOG,                    'last_run.log'),
        (r'C:\benchmark\last_ffmpeg.log', 'last_ffmpeg.log'),
    ]
    for win_path, name in pairs:
        if name in state.received_artifacts:
            continue
        if not ga.file_exists(win_path):
            continue
        data = ga.file_read(win_path)
        if data is None:
            continue
        fname = '{vm}_{host}_{dt}_{name}'.format(
            vm=state.vm, host=state.host_short, dt=state.timestamp, name=name)
        local = Path.cwd() / fname
        local.write_bytes(data)
        state.received_artifacts[name] = local
        info('QGA fallback ← {0} ({1} bytes)'.format(name, len(data)))
    # Status — отдельно парсим (нужен в .received_status)
    if state.received_status is None:
        status_path = state.received_artifacts.get('last_status.json')
        if status_path and status_path.exists():
            try:
                state.received_status = json.loads(status_path.read_text(encoding='utf-8'))
            except Exception as ex:
                warn('QGA fallback: invalid status JSON: {0}'.format(ex))


def _pull_remote_file(ga, remote_path, local_name):
    """Базовый pull одного файла → CWD/<local_name>. Возвращает Path или None."""
    info('Тяну: {0}'.format(remote_path))
    data = ga.file_read(remote_path)
    if data is None:
        warn('Не смог прочитать {0}'.format(remote_path))
        return None
    local = Path.cwd() / local_name
    local.write_bytes(data)
    ok('Сохранён: {0} ({1} bytes)'.format(local, len(data)))
    return local


def _find_remote_via_ps(ga, ps_script, label):
    """Запускает PS-snippet (PS_FIND_LAST_*), возвращает путь файла на VM или None."""
    rc, out, err = ga.ps_wait(ps_script, timeout=20)
    if rc != 0 or not out:
        warn('Не нашёл {0} на VM (PS rc={1}): {2}'.format(label, rc, (err or '').strip()))
        return None
    remote = out.strip().splitlines()[-1].strip()
    if not remote:
        warn('PS вернул пустой путь для {0}'.format(label))
        return None
    return remote


def _pull_last_summary(ga, vm, host_short, config):
    """Найти последнюю benchmark_<dt> папку, выкачать Cyberpunk summary.json
    в CWD c именем `summary_<vm>_<host>_<config>_<YYYYMMDD_HHMMSS>.json`."""
    remote = _find_remote_via_ps(ga, PS_FIND_LAST_SUMMARY, 'Cyberpunk summary')
    if remote is None:
        return None
    dt = time.strftime('%Y%m%d_%H%M%S')
    fname = 'summary_{vm}_{host}_{cfg}_{dt}.json'.format(
        vm=vm, host=host_short, cfg=config, dt=dt)
    return _pull_remote_file(ga, remote, fname)


def _pull_wukong_result(ga, vm, host_short):
    """Найти последний файл результата Wukong-тула, выкачать в CWD как
    `wukong_<vm>_<host>_<YYYYMMDD_HHMMSS>.json`."""
    remote = _find_remote_via_ps(ga, PS_FIND_WUKONG_RESULT, 'Wukong result')
    if remote is None:
        return None
    dt = time.strftime('%Y%m%d_%H%M%S')
    fname = 'wukong_{vm}_{host}_{dt}.json'.format(vm=vm, host=host_short, dt=dt)
    return _pull_remote_file(ga, remote, fname)


def run_all(vm, config, steam_user='', steam_pass='', only_wukong=False,
            imap_host='', email_user='', email_pass='', iterations=1):
    """Один большой флоу: deploy → vnc → bench → pull. Делает всё, что раньше
    делали 5 подкоманд. Steam credentials — опциональны, для Wukong-таска.
    only_wukong (debug-режим ENV ONLY_WUKONG=1) пропускает Cyberpunk.
    imap_host/email_user/email_pass — опциональный auto Steam Guard 2FA."""
    wukong_on = bool(steam_user and steam_pass)
    email_on = bool(imap_host and email_user and email_pass)
    if only_wukong and not wukong_on:
        die('ONLY_WUKONG=1 задан, но нет Steam credentials — нечего запускать. '
            'Передай STEAM_USER=... STEAM_PASS=... через env.', rc=2)
    info('VM={0}  config={1}  load=nvenc (always)  cyberpunk={2}  wukong={3}  steam_guard_auto={4}'.format(
        vm, config,
        'off (only_wukong)' if only_wukong else 'on',
        'on' if wukong_on else 'off',
        'on (IMAP {0}@{1})'.format(email_user, imap_host) if email_on else 'off (manual via VNC)',
    ))

    if not VM_BENCH_PY_LOCAL.exists():
        die('Не нашёл vm_bench.py: {0}'.format(VM_BENCH_PY_LOCAL))
    for tool in ('ip',):
        r = subprocess.run(['which', tool], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            die('Не найдена утилита: {0}'.format(tool))

    ga = GA(vm)
    if not ga.ping():
        die('GA не отвечает (qga-channel не подключён или Windows GA не запущен в VM {0})'.format(vm))
    ok('GA отвечает')

    # ── Пароль gamer (до всего — нужен и для VNC, и для PsExec) ─────────────
    pw = _generate_gamer_pass()
    info('Применяю пароль gamer на VM (net user)...')
    # net.exe ЗОВЁМ НАПРЯМУЮ, не через cmd /c "net user gamer \"PW\"". cmd с
    # 4 кавычками strip'ает первую и последнюю → net видит args ["gamer",
    # "\"PW\""] (с литеральными кавычками!) и ставит пароль С КАВЫЧКАМИ.
    # PsExec потом `-p PW` (без кавычек) → auth failure.
    rc, _, err = ga.exec_wait(r'C:\Windows\System32\net.exe',
                              ['user', 'gamer', pw], timeout=10)
    if rc != 0:
        warn('net user вернул rc={0}, stderr={1}'.format(rc, err))

    _setup_autologon(ga, 'gamer', pw)

    # ── Очистка C:\benchmark\ и пересоздание ────────────────────────────────
    info('Очищаю C:\\benchmark\\ на VM...')
    ga.rmdir_recursive(VM_BENCH_DIR)
    if not ga.mkdir(VM_BENCH_DIR):
        die('Не смог создать {0} на VM — деплой невозможен'.format(VM_BENCH_DIR))

    # ── Хост: iptables flush для VNC-пробивки ───────────────────────────────
    _iptables_flush_if_root()

    # ── Скачать бинари в локальный кэш на хосте ─────────────────────────────
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    info('Кэш бинарей: {0}'.format(CACHE_DIR))
    _download_ffmpeg(CACHE_DIR / 'ffmpeg.exe')
    _download_psexec(CACHE_DIR / 'PsExec.exe')

    # ── Поднимаем HTTP-сервер на весь run_all ───────────────────────────────
    # Один сервер обслуживает GET (deploy ffmpeg/PsExec) + POST (live логи
    # vm_bench.py, push артефактов в конце, /done event вместо QGA-поллинга).
    vm_ip, host_ip = _host_ip_for_vm(ga, vm)
    info('  VM IP: {0} → host src IP: {1}'.format(vm_ip, host_ip))
    port = int(os.environ.get('HTTP_PORT', '8765'))
    host_short = socket.gethostname().split('.')[0]
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    server, state = _start_http_server(str(CACHE_DIR), vm, host_short, timestamp, port)
    host_url = 'http://{0}:{1}'.format(host_ip, port)
    info('HTTP server up: {0} (token={1}...)'.format(host_url, state.token[:8]))

    try:
        # ── Деплой vm_bench.py + ffmpeg.exe (PsExec и .bat-обёртки больше не нужны) ─
        info('Деплой vm_bench.py через GA...')
        _ga_put(ga, VM_BENCH_PY_LOCAL, VM_VMBENCH, 'vm_bench.py')

        _http_put_via_vm(ga, host_ip, port, 'ffmpeg.exe', VM_FFMPEG, 'ffmpeg.exe')

        # Verify
        missing = [p for p in (VM_VMBENCH, VM_FFMPEG) if not ga.file_exists(p)]
        if missing:
            die('После деплоя в VM не хватает:\n  ' + '\n  '.join(missing), rc=2)
        ok('Файлы в VM на месте')

        # ── VNC ─────────────────────────────────────────────────────────────
        _setup_vnc(ga, pw)
        sys.stderr.write('\n')
        sys.stderr.write('{0}══════════════════════════════════════════════════{1}\n'.format(_CYAN, _NC))
        sys.stderr.write('{0}  VNC ready{1}\n'.format(_GREEN, _NC))
        sys.stderr.write('    address : {0}:5900\n'.format(vm_ip))
        sys.stderr.write('    user    : gamer\n')
        sys.stderr.write('    pass    : {0}\n'.format(pw))
        sys.stderr.write('{0}══════════════════════════════════════════════════{1}\n'.format(_CYAN, _NC))
        sys.stderr.write('\n')

        # ── Запуск бенча через Windows Task Scheduler ───────────────────────
        # Никакого PsExec/cmd/.bat — Register-ScheduledTask + Start-ScheduledTask
        # запускают pythonw.exe vm_bench.py args в interactive сессии gamer'а.
        info('Создаю и запускаю scheduled task под gamer (iterations={0})...'.format(iterations))
        py_args = [
            config,
            steam_user, steam_pass,
            '1' if only_wukong else '',
            imap_host, email_user, email_pass,
            host_url, state.token,
            str(iterations),
        ]
        _launch_via_schtasks(ga, py_args)

        # ── Ждём /done через HTTP (живые логи vm_bench пишутся параллельно) ─
        total_timeout = POLL_TIMEOUT_PER_ITER_S * max(1, iterations)
        info('Жду POST /done от vm_bench.py (timeout {0}s, {1} iter × {2}s)...'.format(
            total_timeout, iterations, POLL_TIMEOUT_PER_ITER_S))
        if not state.done_event.wait(timeout=total_timeout):
            warn('HTTP /done не получен за {0}s — fallback на QGA file_read'.format(total_timeout))
            _qga_fallback_pull(ga, state, vm)

        # ── Статус + результат ──────────────────────────────────────────────
        status = state.received_status
        if status is None:
            die('Бенч не отозвался — ни /done через HTTP, ни last_status.json '
                'через QGA. Глянь VNC + pkbench.py cat {0} {1}'.format(vm, VM_RUN_LOG), rc=1)
        sys.stderr.write(json.dumps(status, ensure_ascii=False, indent=2) + '\n')

        exit_code = status.get('exit_code', -1)

        if exit_code != 0:
            warn('Бенч завершился с exit_code={0}'.format(exit_code))
            if status.get('nvenc_died_early'):
                warn('  причина: NVENC ffmpeg умер мид-бенч (rc={0}). '
                     'Глянь last_ffmpeg.log выше.'.format(status.get('nvenc_returncode')))
            # last_run.log уже спулен через /artifact — это файл на диске CWD,
            # уже виден оператору через live-логи. Не дублируем.
            sys.exit(2)

        info('Бенч успешен, last_result.json:')
        result_path = state.received_artifacts.get('last_result.json')
        if result_path and result_path.exists():
            sys.stdout.write(result_path.read_text(encoding='utf-8', errors='replace'))
            sys.stdout.write('\n')
            sys.stdout.flush()
        else:
            warn('last_result.json не получен через HTTP — пробую QGA...')
            result_raw = ga.file_read(VM_RESULT_JSON)
            if result_raw:
                sys.stdout.write(result_raw.decode('utf-8', errors='replace'))
                sys.stdout.write('\n')
                sys.stdout.flush()

        # ── Pull результатов summary/wukong (по-прежнему через QGA) ─────────
        # Эти файлы лежат вне C:\benchmark\ (в CD Projekt Red\... и AppData\...),
        # vm_bench их не push'ит — тянем напрямую через QGA как раньше.
        if status.get('cyberpunk_present'):
            _pull_last_summary(ga, vm, host_short, config)
        elif status.get('cyberpunk_skipped'):
            info('Cyberpunk пропущен (only_wukong) — pull не делаю')
        if status.get('wukong_present'):
            _pull_wukong_result(ga, vm, host_short)
        elif status.get('wukong_error'):
            warn('Wukong не дал результата: см. wukong_error в status.json выше')
        elif status.get('wukong_skipped'):
            info('Wukong пропущен (нет Steam credentials)')

        info('=== Готово ===')
    finally:
        info('Останавливаю HTTP-сервер...')
        try:
            server.shutdown()
            server.server_close()
        except Exception as ex:
            warn('HTTP shutdown упал: {0}'.format(ex))
        _cleanup_schtask(ga)


# ════════════════════════════════════════════════════════════════════════════
#  Отладочный режим: cat
# ════════════════════════════════════════════════════════════════════════════
def _do_cat(vm, path):
    ga = GA(vm)
    data = ga.file_read(path)
    if data is None:
        die('Не смог открыть/прочитать "{0}" на {1}'.format(path, vm), rc=1)
    sys.stdout.buffer.write(data)


# ════════════════════════════════════════════════════════════════════════════
#  main / CLI
# ════════════════════════════════════════════════════════════════════════════
_USAGE = (
    'Usage:\n'
    '    pkbench.py <vm> [config]          — выкатить + VNC + бенч + pull\n'
    '        config: vk (default, 120fps test) | rt (RayTracing) | 2k\n'
    '        NVENC-нагрузка ВСЕГДА (ffmpeg должен прожить весь прогон).\n'
    '\n'
    '        Wukong (опционально): STEAM_USER=... STEAM_PASS=... env\n'
    '        (либо интерактивный запрос, если tty и env пустые).\n'
    '\n'
    '        Авто-ввод Steam Guard 2FA из почты (опционально):\n'
    '          STEAM_GUARD_IMAP_HOST=imap.example.com\n'
    '          STEAM_GUARD_EMAIL=login@example.com\n'
    '          STEAM_GUARD_EMAIL_PASS=<password|app-password>\n'
    '        Без них код придётся ввести вручную через VNC.\n'
    '\n'
    '        Debug — только Wukong (пропустить Cyberpunk): ONLY_WUKONG=1\n'
    '\n'
    '        Прогнать бенч N раз подряд (default 1): ITERATIONS=10\n'
    '        Steam залогинивается один раз, manifest подкладывается один раз,\n'
    '        NVENC поднимается один раз. Результат: {"iterations": N, "runs": [...]}.\n'
    '\n'
    '    pkbench.py cat <vm> <winpath>     — debug: cat файла на VM в stdout\n'
)


def _env_flag(name):
    """1 / true / yes / on (case-insensitive) → True. Иначе False."""
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ('-h', '--help'):
        sys.stderr.write(_USAGE)
        sys.exit(0)

    if argv[0] == 'cat':
        if len(argv) != 3:
            sys.stderr.write(_USAGE)
            die('cat: ожидалось `cat <vm> <winpath>`', rc=2)
        return _do_cat(argv[1], argv[2])

    vm = argv[0]
    config = argv[1] if len(argv) > 1 else 'vk'
    if config not in ('vk', 'rt', '2k'):
        die("config должен быть vk|rt|2k, дано: '{0}'".format(config), rc=2)
    steam_user, steam_pass = _resolve_steam_credentials()
    imap_host, email_user, email_pass = _resolve_email_credentials()
    only_wukong = _env_flag('ONLY_WUKONG')
    try:
        iterations = int(os.environ.get('ITERATIONS', '1'))
    except ValueError:
        die("ITERATIONS должно быть int, дано: '{0}'".format(
            os.environ.get('ITERATIONS')), rc=2)
    if iterations < 1:
        die('ITERATIONS должно быть >= 1', rc=2)
    run_all(vm, config, steam_user, steam_pass,
            only_wukong=only_wukong,
            imap_host=imap_host, email_user=email_user, email_pass=email_pass,
            iterations=iterations)


if __name__ == '__main__':
    main()
