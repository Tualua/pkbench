#!/usr/bin/env python3
# pkbench_multi.py — параллельный orchestrator над pkbench.py.
#
# Запускает N независимых pkbench.py процессов под разные VM, каждый со
# своим HTTP_PORT (8765 + index). Per-VM Steam credentials через env-vars
# с суффиксом _<VM>:
#     STEAM_USER_<vm>, STEAM_PASS_<vm>
#     STEAM_GUARD_EMAIL_<vm>, STEAM_GUARD_EMAIL_PASS_<vm>, STEAM_GUARD_IMAP_HOST_<vm>
#     ITERATIONS_<vm>, ONLY_WUKONG_<vm>
# Если суффикс-варианта нет — fallback на базовый STEAM_USER и т.д.
#
# VM-имена с `-` в env-vars нелегальны, заменяем на `_`: vm-043 → STEAM_USER_vm_043.
#
# Usage:
#     sudo -E ./pkbench_multi.py <config> <vm1> [vm2] [vm3] ...
#
# Все child stderr префиксятся `[<vm>] ` чтобы не путались. В конце таблица
# FPS из {vm}_*_last_result.json в CWD.
#
# Прогрев кэша: ffmpeg.exe (~200MB) скачивается в vm_deploy_cache/ ДО fanout'a
# (atomic через tmp+rename). Дальше child pkbench.py видят файл и пропускают
# download — race'а нет.
"""pkbench_multi.py — parallel orchestrator над pkbench.py."""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path


HERE        = Path(__file__).resolve().parent
PKBENCH     = HERE / 'pkbench.py'
CACHE_DIR   = HERE / 'vm_deploy_cache'    # должно совпадать с CACHE_DIR в pkbench.py
FFMPEG_EXE  = CACHE_DIR / 'ffmpeg.exe'
FFMPEG_URL  = ('https://github.com/BtbN/FFmpeg-Builds/releases/download/'
               'latest/ffmpeg-master-latest-win64-gpl.zip')
FIO_MSI          = CACHE_DIR / 'fio.msi'                 # ставится через msiexec на VM
FIO_REPO         = 'axboe/fio'
FIO_RELEASES_API = 'https://api.github.com/repos/{0}/releases/latest'.format(FIO_REPO)
BASE_PORT   = int(os.environ.get('HTTP_PORT', '8765'))
STAGGER_S   = 2.0     # cache уже прогрет — race'а нет, минимальный stagger
PER_VM_ENV  = (
    'STEAM_USER', 'STEAM_PASS',
    'STEAM_GUARD_EMAIL', 'STEAM_GUARD_EMAIL_PASS', 'STEAM_GUARD_IMAP_HOST',
    'ITERATIONS', 'ONLY_WUKONG', 'ONLY_DISK', 'SKIP_DISK',
)


def _human_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return '{0:.1f} {1}'.format(n, unit)
        n /= 1024.0
    return '{0:.1f} TB'.format(n)


def _url_download(url, dst, timeout=300):
    """urlretrieve с SSL-fallback на unverified context. OL7 ca-bundle устарел
    и не верифицирует Let's Encrypt новые промежуточные серты (bsdio.com,
    свежие github CDN cert chains). URL'ы у нас pinned в коде, MITM-risk
    минимальный."""
    import ssl
    def _do(ctx):
        kwargs = {'timeout': timeout}
        if ctx is not None:
            kwargs['context'] = ctx
        with urllib.request.urlopen(url, **kwargs) as r:
            with open(str(dst), 'wb') as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
    try:
        _do(None)
    except (ssl.SSLError, urllib.error.URLError) as ex:
        if 'CERTIFICATE_VERIFY' not in str(ex) and 'SSL' not in str(ex):
            raise
        sys.stderr.write('[prewarm] SSL verify FAIL для {0} — retry unverified\n'.format(url))
        _do(ssl._create_unverified_context())


def prewarm_ffmpeg():
    """Скачать ffmpeg.exe в CACHE_DIR/ если ещё нет. Atomic через tmp+rename,
    чтобы parallel child pkbench.py точно увидели готовый файл (не partial).
    Дублируем logic из pkbench._download_ffmpeg чтобы не тащить весь pkbench
    модуль (он тянет libvirt и шлёт side-effects при import)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if FFMPEG_EXE.exists():
        sys.stderr.write('[prewarm] ffmpeg.exe уже в кэше ({0})\n'.format(
            _human_size(FFMPEG_EXE.stat().st_size)))
        sys.stderr.flush()
        return
    sys.stderr.write('[prewarm] Качаю ffmpeg (BtbN win64-gpl, ~200MB)...\n')
    sys.stderr.flush()
    zip_path = FFMPEG_EXE.with_suffix('.zip')
    _url_download(FFMPEG_URL, zip_path)
    sys.stderr.write('[prewarm] Распаковываю ffmpeg.exe...\n')
    sys.stderr.flush()
    tmp_exe = FFMPEG_EXE.with_suffix('.tmp')
    with zipfile.ZipFile(str(zip_path)) as zf:
        members = [n for n in zf.namelist() if n.endswith('/bin/ffmpeg.exe')]
        if not members:
            sys.stderr.write('[prewarm] FAIL: в архиве нет */bin/ffmpeg.exe\n')
            sys.exit(2)
        with zf.open(members[0]) as src, open(str(tmp_exe), 'wb') as dst:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                dst.write(buf)
    tmp_exe.replace(FFMPEG_EXE)   # atomic rename on POSIX
    zip_path.unlink()
    sys.stderr.write('[prewarm] ffmpeg.exe готов ({0})\n'.format(
        _human_size(FFMPEG_EXE.stat().st_size)))
    sys.stderr.flush()


def _resolve_fio_msi_url():
    """GitHub API → URL latest fio-*-x64.msi asset из axboe/fio releases."""
    import ssl
    req = urllib.request.Request(
        FIO_RELEASES_API,
        headers={'Accept': 'application/vnd.github+json',
                 'User-Agent': 'pkbench-multi/1.0'},
    )
    try:
        r = urllib.request.urlopen(req, timeout=20)
    except (ssl.SSLError, urllib.error.URLError) as ex:
        if 'CERTIFICATE_VERIFY' not in str(ex) and 'SSL' not in str(ex):
            raise
        r = urllib.request.urlopen(req, timeout=20,
                                   context=ssl._create_unverified_context())
    with r:
        data = json.loads(r.read().decode('utf-8'))
    tag = data.get('tag_name', '?')
    for a in data.get('assets') or []:
        if (a.get('name') or '').lower().endswith('-x64.msi'):
            return tag, a['name'], a['browser_download_url']
    sys.stderr.write('[prewarm] FAIL: в {0} ({1}) нет *-x64.msi assets\n'.format(
        FIO_REPO, tag))
    sys.exit(2)


def prewarm_fio():
    """Скачать fio.msi (axboe/fio GitHub release) в кэш. Установка идёт на
    VM через msiexec /i /qn — никакого extract на хосте, никаких p7zip.
    Atomic tmp+rename."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if FIO_MSI.exists():
        sys.stderr.write('[prewarm] fio.msi уже в кэше ({0})\n'.format(
            _human_size(FIO_MSI.stat().st_size)))
        sys.stderr.flush()
        return
    sys.stderr.write('[prewarm] Запрос latest release {0}...\n'.format(FIO_REPO))
    sys.stderr.flush()
    tag, name, url = _resolve_fio_msi_url()
    sys.stderr.write('[prewarm] Качаю {0} ({1})...\n'.format(name, tag))
    sys.stderr.flush()
    tmp = FIO_MSI.with_suffix('.tmp')
    _url_download(url, tmp)
    tmp.replace(FIO_MSI)
    sys.stderr.write('[prewarm] fio.msi готов ({0})\n'.format(
        _human_size(FIO_MSI.stat().st_size)))
    sys.stderr.flush()


def _env_safe(name):
    return re.sub(r'[^A-Za-z0-9_]', '_', name)


def collect_env(vm):
    """Возвращает (env, sources) — env для child'a и dict {key: 'suffix'|'global'|None}
    откуда что взялось (для diagnostics)."""
    env = dict(os.environ)
    sources = {}
    safe = _env_safe(vm)
    for k in PER_VM_ENV:
        suff = '{0}_{1}'.format(k, safe)
        if suff in env:
            env[k] = env[suff]
            sources[k] = 'suffix'
        elif env.get(k):
            sources[k] = 'global'
        else:
            sources[k] = None
    return env, sources


def preflight_creds(vms):
    """Печатает resolved STEAM_USER per VM. Fail-fast если 2+ VM resolved'ятся
    в один и тот же STEAM_USER (Steam кикает первую сессию когда вторая логинит
    тот же account → параллельный Wukong невозможен).

    Если STEAM_USER пустой у всех (Wukong-задача отключится) — это OK,
    только Cyberpunk прогоним."""
    sys.stderr.write('=== Steam credentials per VM ===\n')
    seen = {}     # steam_user → vm который первым взял
    has_dup = False
    for vm in vms:
        env, sources = collect_env(vm)
        user = env.get('STEAM_USER', '')
        src  = sources.get('STEAM_USER')
        if not user:
            sys.stderr.write('  {0:14s}  STEAM_USER=(empty) → Wukong будет пропущен\n'.format(vm))
            continue
        marker = 'suffix' if src == 'suffix' else 'GLOBAL (fallback)'
        sys.stderr.write('  {0:14s}  STEAM_USER={1}  [{2}]\n'.format(vm, user, marker))
        if user in seen:
            sys.stderr.write('    !! ДУБЛИКАТ: тот же user уже у {0}\n'.format(seen[user]))
            has_dup = True
        else:
            seen[user] = vm
    if has_dup:
        sys.stderr.write(
            '\nFAIL: один STEAM_USER используется на нескольких VM. '
            'Steam кикнет первую сессию когда вторая залогинится — параллельный '
            'Wukong невозможен. Задай уникальные креды через суффикс-env:\n'
            '    export STEAM_USER_<vm1>=acc1   STEAM_PASS_<vm1>=...\n'
            '    export STEAM_USER_<vm2>=acc2   STEAM_PASS_<vm2>=...\n'
            'Либо отключи Wukong: `unset STEAM_USER STEAM_PASS` перед запуском.\n')
        sys.exit(2)


def run_one(vm, config, port, results, lock):
    env, _ = collect_env(vm)
    env['HTTP_PORT'] = str(port)
    pfx = '[{0}] '.format(vm)
    sys.stderr.write(pfx + '== start config={0} port={1}\n'.format(config, port))
    sys.stderr.flush()
    proc = subprocess.Popen(
        [sys.executable, str(PKBENCH), vm, config],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
    )
    for line in proc.stdout:
        sys.stderr.write(pfx + line)
        sys.stderr.flush()
    rc = proc.wait()
    with lock:
        results[vm] = rc
    sys.stderr.write(pfx + '== done rc={0}\n'.format(rc))
    sys.stderr.flush()


def _fmt_fps(v):
    if v is None:
        return '?'
    try:
        return str(int(round(float(v))))
    except (TypeError, ValueError):
        return '?'


def find_latest_result(vm):
    cwd = Path('.')
    pat = re.compile(r'^' + re.escape(vm) + r'_.*_last_result\.json$')
    matches = [p for p in cwd.iterdir() if p.is_file() and pat.match(p.name)]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def summarize_one(vm, rc):
    status = 'OK' if rc == 0 else 'FAIL(rc={0})'.format(rc)
    rfile = find_latest_result(vm)
    if not rfile:
        return '{0:14s}  {1}'.format(vm, status)
    try:
        data = json.loads(rfile.read_text(encoding='utf-8'))
    except Exception as ex:
        return '{0:14s}  {1}  (result parse err: {2})'.format(vm, status, ex)
    cp_avg, cp_min, wk_avg, wk_min = [], [], [], []
    for run in data.get('runs') or []:
        cp = run.get('cyberpunk')
        wk = run.get('wukong')
        if cp:
            cp_avg.append(_fmt_fps(cp.get('averageFps')))
            cp_min.append(_fmt_fps(cp.get('minFps')))
        if wk:
            wk_avg.append(_fmt_fps(wk.get('FPSAvg')))
            wk_min.append(_fmt_fps(wk.get('FPSMin')))
    parts = []
    if cp_avg:
        parts.append('cp avg=[{0}] min=[{1}]'.format(','.join(cp_avg), ','.join(cp_min)))
    if wk_avg:
        parts.append('wk avg=[{0}] min=[{1}]'.format(','.join(wk_avg), ','.join(wk_min)))
    patterns = (data.get('disk') or {}).get('patterns') or {}
    if patterns:
        # Берём seq-1m-q8 для bandwidth, rnd-4k-q32 для IOPS — самые показательные.
        seq = patterns.get('seq-1m-q8') or {}
        rnd = patterns.get('rnd-4k-q32') or {}
        sr = (seq.get('read')  or {}).get('bw_mb_s') or 0
        sw = (seq.get('write') or {}).get('bw_mb_s') or 0
        rr = (rnd.get('read')  or {}).get('iops')    or 0
        rw = (rnd.get('write') or {}).get('iops')    or 0
        parts.append('disk seqQ8 R/W={0}/{1}MB/s rndQ32 R/W={2}/{3}kIOPS'.format(
            int(sr), int(sw), int(rr / 1000), int(rw / 1000)))
    return '{0:14s}  {1}  {2}'.format(vm, status, '  '.join(parts))


def print_summary(vms, results):
    sys.stderr.write('\n=== Multi-VM summary ===\n')
    for vm in vms:
        sys.stderr.write('  ' + summarize_one(vm, results.get(vm)) + '\n')
    sys.stderr.flush()


_USAGE = """\
Usage:
    sudo -E ./pkbench_multi.py <config> <vm1> [vm2] [vm3] ...

config: vk | rt | 2k

Per-VM env-vars: STEAM_USER_<vm>, STEAM_PASS_<vm>,
                 STEAM_GUARD_EMAIL_<vm>, STEAM_GUARD_EMAIL_PASS_<vm>,
                 STEAM_GUARD_IMAP_HOST_<vm>,
                 ITERATIONS_<vm>, ONLY_WUKONG_<vm>
(fallback на STEAM_USER/STEAM_PASS/... если суффикс-варианта нет)

HTTP_PORT (default 8765) — порт первой VM; вторая получит +1 и т.д.
"""


def main():
    argv = sys.argv[1:]
    if len(argv) < 2 or argv[0] in ('-h', '--help'):
        sys.stderr.write(_USAGE)
        sys.exit(2)
    config = argv[0]
    if config not in ('vk', 'rt', '2k'):
        sys.stderr.write("config должен быть vk|rt|2k, дано: '{0}'\n".format(config))
        sys.exit(2)
    vms = argv[1:]
    if len(set(vms)) != len(vms):
        sys.stderr.write('Дубликаты VM в списке: {0}\n'.format(vms))
        sys.exit(2)

    sys.stderr.write('=== pkbench_multi: {0} VM, config={1} ===\n'.format(len(vms), config))

    preflight_creds(vms)
    prewarm_ffmpeg()
    prewarm_fio()

    results = {}
    lock = threading.Lock()
    threads = []
    for i, vm in enumerate(vms):
        port = BASE_PORT + i
        t = threading.Thread(target=run_one,
                             args=(vm, config, port, results, lock),
                             daemon=False)
        t.start()
        threads.append(t)
        if i < len(vms) - 1:
            time.sleep(STAGGER_S)
    for t in threads:
        t.join()

    print_summary(vms, results)
    if any(rc != 0 for rc in results.values()):
        sys.exit(1)


if __name__ == '__main__':
    main()
