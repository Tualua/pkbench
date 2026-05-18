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
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
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
        (UTF-16LE/base64) — обходит quoting hell с длинными многострочными
        выражениями."""
        b64 = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        return self.exec_wait(
            'powershell.exe',
            ['-NoProfile', '-NonInteractive', '-EncodedCommand', b64],
            timeout=timeout,
        )

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
BENCH_PSEXEC_BAT = (
    '@echo off\r\n'
    'REM bench_psexec.bat — generated by pkbench.py at deploy stage. Не редактируй вручную.\r\n'
    'REM Args: 1=gamer-pw 2=cfg 3=steam_user 4=steam_pass 5=only_wukong\r\n'
    'REM       6=imap_host 7=email_user 8=email_pass (auto Steam Guard).\r\n'
    'REM Любые из 3-8 могут быть пустыми; vm_bench.py их корректно обработает.\r\n'
    '"C:\\benchmark\\PsExec.exe" -accepteula -u gamer -p "%~1" -i 1 -d '
    'cmd.exe /c "call C:\\benchmark\\run_via_ga_launcher.bat %~2 %~3 %~4 %~5 %~6 %~7 %~8" '
    '> "C:\\benchmark\\psexec.log" 2>&1\r\n'
)

# run_via_ga_launcher.bat: cmd → python.exe в уже-аутентифицированной сессии.
# Без этого слоя CreateProcessAsUser для python.exe фейлится.
RUN_VIA_GA_LAUNCHER_BAT = (
    '@echo off\r\n'
    'REM run_via_ga_launcher.bat — generated by pkbench.py at deploy stage.\r\n'
    'REM Args: 1=config 2=steam_user 3=steam_pass 4=only_wukong\r\n'
    'REM       5=imap_host 6=email_user 7=email_pass (auto Steam Guard).\r\n'
    '"C:\\Program Files (x86)\\Python36-32\\python.exe" '
    '"C:\\benchmark\\vm_bench.py" "%~1" "%~2" "%~3" "%~4" "%~5" "%~6" "%~7"\r\n'
)


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
VM_PSEXEC    = r'C:\benchmark\PsExec.exe'
VM_FFMPEG    = r'C:\benchmark\ffmpeg.exe'
VM_VMBENCH   = r'C:\benchmark\vm_bench.py'
VM_BENCH_BAT = r'C:\benchmark\bench_psexec.bat'
VM_LAUNCHER  = r'C:\benchmark\run_via_ga_launcher.bat'
VM_PSEXEC_LOG    = r'C:\benchmark\psexec.log'
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


def _start_http_server(serve_dir, port):
    """Поднять `python3 -m http.server` в фоне. Возвращает Popen.
    --directory нет в 3.6 → cwd= в Popen."""
    # ss -ltn проверка порта — быстрее чем bind→fail
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('0.0.0.0', port))
        s.close()
    except OSError:
        die('Порт {0} уже занят на хосте (используй HTTP_PORT=NNNN)'.format(port))

    log_fh = tempfile.NamedTemporaryFile(prefix='pkbench-http.', suffix='.log',
                                          delete=False)
    proc = subprocess.Popen(
        [sys.executable, '-m', 'http.server', str(port), '--bind', '0.0.0.0'],
        cwd=serve_dir, stdout=log_fh, stderr=subprocess.STDOUT,
    )
    time.sleep(1)
    if proc.poll() is not None:
        log_fh.close()
        with open(log_fh.name) as f:
            warn('python http.server упал. stderr/stdout:\n' + f.read())
        die('HTTP-сервер не стартанул')
    return proc, log_fh.name


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
POLL_TIMEOUT_S  = 30 * 60   # 2 итерации × ~8 мин + запас

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
            imap_host='', email_user='', email_pass=''):
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

    # ── Деплой мелочи через GA + бинарей через HTTP ─────────────────────────
    info('Деплой vm_bench.py + .bat обёрток через GA...')
    _ga_put(ga, VM_BENCH_PY_LOCAL, VM_VMBENCH, 'vm_bench.py')
    _ga_put_text(ga, BENCH_PSEXEC_BAT,        VM_BENCH_BAT, 'bench_psexec.bat')
    _ga_put_text(ga, RUN_VIA_GA_LAUNCHER_BAT, VM_LAUNCHER,  'run_via_ga_launcher.bat')

    info('HTTP для больших бинарей (ffmpeg, PsExec)...')
    vm_ip, host_ip = _host_ip_for_vm(ga, vm)
    info('  VM IP: {0} → host src IP: {1}'.format(vm_ip, host_ip))
    port = int(os.environ.get('HTTP_PORT', '8765'))
    http_proc, http_log = _start_http_server(str(CACHE_DIR), port)
    try:
        ok('HTTP server pid={0} port={1}'.format(http_proc.pid, port))
        _http_put_via_vm(ga, host_ip, port, 'ffmpeg.exe', VM_FFMPEG, 'ffmpeg.exe')
        _http_put_via_vm(ga, host_ip, port, 'PsExec.exe', VM_PSEXEC, 'PsExec.exe')
    finally:
        http_proc.terminate()
        try:
            http_proc.wait(timeout=5)
        except Exception:
            http_proc.kill()
        try:
            os.unlink(http_log)
        except Exception:
            pass

    # Verify
    missing = [p for p in (VM_VMBENCH, VM_BENCH_BAT, VM_LAUNCHER, VM_FFMPEG, VM_PSEXEC)
               if not ga.file_exists(p)]
    if missing:
        die('После деплоя в VM не хватает:\n  ' + '\n  '.join(missing), rc=2)
    ok('Файлы в VM на месте')

    # ── VNC ─────────────────────────────────────────────────────────────────
    _setup_vnc(ga, pw)

    # Печать VNC connect info — крупным баннером, чтобы было видно в потоке логов.
    sys.stderr.write('\n')
    sys.stderr.write('{0}══════════════════════════════════════════════════{1}\n'.format(_CYAN, _NC))
    sys.stderr.write('{0}  VNC ready{1}\n'.format(_GREEN, _NC))
    sys.stderr.write('    address : {0}:5900\n'.format(vm_ip))
    sys.stderr.write('    user    : gamer\n')
    sys.stderr.write('    pass    : {0}\n'.format(pw))
    sys.stderr.write('{0}══════════════════════════════════════════════════{1}\n'.format(_CYAN, _NC))
    sys.stderr.write('\n')

    # ── Запуск бенча через PsExec ───────────────────────────────────────────
    info('Запуск бенча: PsExec → cmd → launcher → vm_bench.py')
    # 8 args в bench_psexec.bat: gamer-pass, config, steam_user, steam_pass,
    # only_wukong, imap_host, email_user, email_pass.
    # Пустые steam_user/pass → vm_bench.py пропустит Wukong.
    # only_wukong='1' → vm_bench.py пропустит Cyberpunk (debug).
    # Пустые imap_*/email_* → 2FA-код придётся ввести вручную через VNC.
    pid = ga.exec('cmd.exe', ['/c', VM_BENCH_BAT, pw, config,
                              steam_user, steam_pass, '1' if only_wukong else '',
                              imap_host, email_user, email_pass])
    if pid is None:
        die('guest-exec bench_psexec.bat не отдал PID', rc=3)

    # PsExec -d пишет диагностику в psexec.log и сразу выходит — даём отстояться,
    # читаем для дебага.
    time.sleep(3)
    info('psexec.log:')
    pse_log = ga.file_read(VM_PSEXEC_LOG)
    if pse_log:
        for line in pse_log.decode('utf-8', errors='replace').splitlines():
            sys.stderr.write('    ' + line + '\n')
    else:
        warn('  psexec.log не появился — bench_psexec.bat не запустился вообще?')

    # ── Поллинг last_status.json ────────────────────────────────────────────
    info('Жду last_status.json (timeout {0}s, poll {1}s)...'.format(
        POLL_TIMEOUT_S, POLL_INTERVAL_S))
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        if ga.file_exists(VM_STATUS_JSON):
            ok('  → last_status.json появился')
            break
        time.sleep(POLL_INTERVAL_S)
    else:
        die('Таймаут: last_status.json так и не появился. Глянь VNC + '
            'pkbench.py cat {0} {1}'.format(vm, VM_RUN_LOG), rc=1)

    # ── Статус + результат ──────────────────────────────────────────────────
    status_raw = ga.file_read(VM_STATUS_JSON)
    if status_raw is None:
        die('Не смог прочитать last_status.json', rc=1)
    status_text = status_raw.decode('utf-8', errors='replace')
    sys.stderr.write(status_text + '\n')

    try:
        status = json.loads(status_text)
    except Exception as ex:
        # die() делает sys.exit, но pyright это не знает — оборачиваем чтобы
        # `status` не был «possibly unbound» ниже.
        die('Не смог распарсить last_status.json: {0}'.format(ex), rc=1)
        return   # unreachable, для type-checker'а
    exit_code = status.get('exit_code', -1)

    if exit_code != 0:
        warn('Бенч завершился с exit_code={0}, тяну last_run.log...'.format(exit_code))
        if status.get('nvenc_died_early'):
            warn('  причина: NVENC ffmpeg умер мид-бенч (rc={0}). '
                 'Глянь: pkbench.py cat {1} \'C:\\benchmark\\last_ffmpeg.log\''
                 .format(status.get('nvenc_returncode'), vm))
        run_log = ga.file_read(VM_RUN_LOG)
        if run_log:
            sys.stderr.write(run_log.decode('utf-8', errors='replace'))
        else:
            warn('(last_run.log недоступен)')
        sys.exit(2)

    info('Бенч успешен, last_result.json:')
    result_raw = ga.file_read(VM_RESULT_JSON)
    if result_raw is None:
        die('Не смог прочитать last_result.json', rc=1)
    sys.stdout.write(result_raw.decode('utf-8', errors='replace'))
    if not result_raw.endswith(b'\n'):
        sys.stdout.write('\n')
    sys.stdout.flush()

    # ── Pull результатов в CWD с именованием ────────────────────────────────
    host_short = socket.gethostname().split('.')[0]
    if status.get('cyberpunk_present'):
        _pull_last_summary(ga, vm, host_short, config)
    elif status.get('cyberpunk_skipped'):
        info('Cyberpunk пропущен (only_wukong) — pull не делаю')
    # Wukong: тянем только если бенч-сторона его реально запускала. wukong_present
    # == True означает что vm_bench записал result_path в last_result.json.
    # Если skipped или error — файла Tool\<...> может вообще не быть, не дёргаем PS.
    if status.get('wukong_present'):
        _pull_wukong_result(ga, vm, host_short)
    elif status.get('wukong_error'):
        warn('Wukong не дал результата: см. wukong_error в status.json выше')
    elif status.get('wukong_skipped'):
        info('Wukong пропущен (нет Steam credentials)')

    info('=== Готово ===')


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
    run_all(vm, config, steam_user, steam_pass,
            only_wukong=only_wukong,
            imap_host=imap_host, email_user=email_user, email_pass=email_pass)


if __name__ == '__main__':
    main()
