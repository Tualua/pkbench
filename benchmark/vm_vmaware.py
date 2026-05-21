#!/usr/bin/env python3
# vm_vmaware.py — прогнать VMAware (kernelwernel/VMAware) на bench-VM.
#
# Что делает:
#   1. Хост: GitHub API → URL последнего vmaware.exe asset'a
#   2. VM: PowerShell Invoke-WebRequest качает exe напрямую с GitHub
#      (не льём через QGA — exe-файлы через guest-file-write иногда
#      ломаются, причина не выяснена; Defender-perception тоже бывает)
#   3. VM: запускает `vmaware.exe --json`, stdout → файл на диске
#   4. Хост: тянет результат через QGA file_read → CWD
"""vm_vmaware.py — download (via VM PS) + run VMAware on VM, pull JSON."""

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pkbench as pk


VMAWARE_REPO  = 'kernelwernel/VMAware'
RELEASES_API  = 'https://api.github.com/repos/{0}/releases/latest'.format(VMAWARE_REPO)
VM_EXE        = r'C:\temp\vmaware.exe'
VM_RESULT     = r'C:\temp\vmaware_result.json'
DOWNLOAD_TIMEOUT_S = 120
RUN_TIMEOUT_S      = 300


def fetch_latest_exe_url():
    """GitHub API → (tag, asset_name, download_url) основного `vmaware.exe`.
    Берём ИМЕННО vmaware.exe (без суффиксов вроде -vbox-compat)."""
    req = urllib.request.Request(
        RELEASES_API,
        headers={'Accept': 'application/vnd.github+json',
                 'User-Agent': 'pkbench-vmaware/1.0'},
    )
    with pk._url_open(req, timeout=20) as r:
        data = json.loads(r.read().decode('utf-8'))
    tag = data.get('tag_name', '?')
    assets = data.get('assets') or []
    for a in assets:
        if (a.get('name') or '').lower() == 'vmaware.exe':
            return tag, a['name'], a['browser_download_url']
    pk.die('В релизе {0} нет asset `vmaware.exe`. Список .exe: {1}'.format(
        tag, [a.get('name') for a in assets if (a.get('name') or '').lower().endswith('.exe')]))


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ('-h', '--help'):
        sys.stderr.write(
            'Usage: vm_vmaware.py <vm>\n'
            '\n'
            'Резолвит latest release kernelwernel/VMAware на хосте, VM сама\n'
            'качает vmaware.exe через PowerShell Invoke-WebRequest напрямую\n'
            'с GitHub, запускает `vmaware.exe --json` (stdout → файл на VM),\n'
            'хост тянет результат в CWD как vmaware_<vm>_<dt>.json.\n')
        sys.exit(2)
    if len(argv) != 1:
        pk.die('Ожидалось `vm_vmaware.py <vm>`, дано: ' + ' '.join(argv), rc=2)
    vm = argv[0]

    pk.info('Запрос latest release {0}...'.format(VMAWARE_REPO))
    tag, name, url = fetch_latest_exe_url()
    pk.ok('Latest: {0} → {1}'.format(tag, url))

    ga = pk.GA(vm)
    if not ga.ping():
        pk.die('GA не отвечает на VM {0}'.format(vm))
    pk.ok('GA отвечает')

    pk.info('Гарантирую C:\\temp\\ на VM...')
    if not ga.mkdir(r'C:\temp'):
        pk.die('Не смог создать C:\\temp на VM')

    # Возможно vmaware.exe висит зомби от прошлой попытки (например прошлый
    # запуск отвалился по timeout, exe ещё работает) — таскилл прежде чем
    # пытаться перезаписать файл.
    pk.info('Гашу прошлые vmaware.exe (если есть)...')
    ga.exec_wait(r'C:\Windows\System32\taskkill.exe',
                 ['/F', '/IM', 'vmaware.exe'], timeout=10)

    # Чистим старые артефакты перед запуском, чтобы не подхватить stale-результат.
    ga.delete(VM_EXE)
    ga.delete(VM_RESULT)

    # VM качает vmaware.exe сама — PS Invoke-WebRequest напрямую с GitHub.
    # GitHub release URLs возвращают 302 → CDN, IWR с -UseBasicParsing
    # следует редиректам по дефолту.
    pk.info('VM качает vmaware.exe через PS (timeout {0}s)...'.format(DOWNLOAD_TIMEOUT_S))
    dl_script = (
        "$ProgressPreference = 'SilentlyContinue'\n"
        "Invoke-WebRequest -Uri '{url}' -OutFile '{dst}' -UseBasicParsing\n"
        "if (-not (Test-Path '{dst}')) {{ throw 'после IWR файла нет на диске' }}\n"
        "$sz = (Get-Item '{dst}').Length\n"
        "Write-Output ('OK ' + $sz)\n"
    ).format(url=url, dst=VM_EXE)
    rc, out, err = ga.ps_wait(dl_script, timeout=DOWNLOAD_TIMEOUT_S)
    if rc != 0:
        pk.die('IWR не справился rc={0}: {1}'.format(rc, (err or out or '').strip()))
    pk.ok('vmaware.exe на VM: ' + (out or '').strip())

    # Запуск vmaware напрямую через GA exec (qemu-ga = LocalSystem → SYSTEM-
    # привилегии, выше admin'a; не через PS, чтобы не словить ограничения PS-
    # хоста). `-o <file>` заставляет vmaware писать JSON сразу в файл —
    # никаких PS-стримов и BOM-проблем.
    pk.info('Запускаю vmaware.exe -o <file> --json (timeout {0}s)...'.format(RUN_TIMEOUT_S))
    rc, out, err = ga.exec_wait(VM_EXE, ['-o', VM_RESULT, '--json'],
                                timeout=RUN_TIMEOUT_S)
    if rc is None:
        pk.die('vmaware.exe не завершился за {0}s (timeout)'.format(RUN_TIMEOUT_S))
    if rc != 0:
        # vmaware exit code не обязательно фейл (бит может сигналить detection
        # confidence). Пробуем всё равно прочитать result.
        pk.warn('vmaware.exe rc={0}, stderr:\n{1}'.format(rc, (err or '').strip()))

    if not ga.file_exists(VM_RESULT):
        pk.die('Результат не появился: {0} (см. stderr выше из rc-warn)'.format(VM_RESULT))

    pk.info('Тяну результат с VM через QGA...')
    raw = ga.file_read(VM_RESULT)
    if raw is None:
        pk.die('file_read({0}) вернул None'.format(VM_RESULT))
    text = raw.decode('utf-8', errors='replace')

    # Sanity-валидация (не fatal — если не JSON, сохраняем raw).
    try:
        json.loads(text)
        pk.ok('JSON валидный')
    except Exception as ex:
        pk.warn('stdout не валидный JSON: {0} — сохраняю as-is'.format(ex))

    dt = time.strftime('%Y%m%d_%H%M%S')
    out_name = 'vmaware_{0}_{1}.json'.format(vm, dt)
    out_path = Path.cwd() / out_name
    out_path.write_text(text, encoding='utf-8')
    pk.ok('Результат: {0} ({1} bytes)'.format(out_name, len(text.encode('utf-8'))))


if __name__ == '__main__':
    main()
