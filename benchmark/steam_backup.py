#!/usr/bin/env python3
"""
steam_backup.py — снять/подложить Steam Trusted Device файлы на VM через QGA.

Цель: один раз залогиниться в Steam (с 2FA) через VNC, забэкапить токены этой
машины, потом подкладывать в свежие VM — Steam узнаёт «Trusted Device» и
больше 2FA не спрашивает.

Использование:
    ./steam_backup.py backup  <vm>                       — снять с <vm>
    ./steam_backup.py restore <vm>                       — положить в <vm> из своего бэкапа
    ./steam_backup.py restore <vm> --from <other_vm>     — положить в <vm> из бэкапа <other_vm>

Бэкапится (Steam установлен на F:\\launch\\Steam — не стандартный C:\\Program Files):
    F:\\launch\\Steam\\config\\loginusers.vdf  — список аккаунтов + refresh-токены
    F:\\launch\\Steam\\config\\config.vdf      — общая конфигурация (auth cache)
    F:\\launch\\Steam\\ssfn<digits>            — Trusted Device tokens (обходят 2FA)

Путь Steam меняется через ENV STEAM_DIR=... — например для playkey_pro VM (D:\\Steam).

Бэкапы кладутся в ./steam_trusted/<vm>/ (рядом со скриптом). Эта папка
не должна попадать в git: ssfn-файлы = ключи аккаунта.

ВАЖНО:
    - Steam НА VM должен быть остановлен перед бэкапом и до рестарта после
      restore'а, иначе ssfn может быть locked / Steam перезапишет.
    - Restore поднимает Trusted-status только для тех аккаунтов, чьи токены
      лежат в loginusers.vdf. Перезапуск Steam — обязателен после restore.
"""

import os
import sys
from pathlib import Path

# GA класс реюзаем из pkbench.py — libvirt-python + QGA-обёртки. Импорт
# срабатывает без побочных эффектов (main() под `if __name__`).
from pkbench import GA, info, ok, warn, die

# На наших VM Steam-клиент установлен на F:\launch\Steam, не в C:\Program Files.
# Для playkey_pro-VM путь D:\Steam. Переопределяется через STEAM_DIR=...
STEAM_DIR        = os.environ.get('STEAM_DIR', r'F:\launch\Steam')
STEAM_CONFIG_DIR = STEAM_DIR + r'\config'
BACKUP_ROOT      = Path(__file__).resolve().parent / 'steam_trusted'


def _list_ssfn(ga):
    """Список абсолютных путей всех ssfn* в Steam-папке."""
    rc, out, err = ga.ps_wait(
        "Get-ChildItem -LiteralPath '{0}' -File -ErrorAction SilentlyContinue "
        "| Where-Object {{ $_.Name -like 'ssfn*' }} "
        "| ForEach-Object {{ $_.FullName }}".format(STEAM_DIR),
        timeout=15,
    )
    if rc != 0:
        warn('Не удалось перечислить ssfn-файлы: rc={0} {1}'.format(rc, err))
        return []
    return [ln.strip() for ln in (out or '').splitlines() if ln.strip()]


def cmd_backup(vm):
    ga = GA(vm)
    if not ga.ping():
        die('GA не отвечает')

    out_dir = BACKUP_ROOT / vm
    out_dir.mkdir(parents=True, exist_ok=True)
    info('Backup VM={0} → {1}'.format(vm, out_dir))

    # Базовый сет — config-файлы.
    targets = [
        (STEAM_CONFIG_DIR + r'\loginusers.vdf', 'loginusers.vdf'),
        (STEAM_CONFIG_DIR + r'\config.vdf',     'config.vdf'),
    ]
    # + динамически: все ssfn<digits>
    ssfn = _list_ssfn(ga)
    if not ssfn:
        warn('ssfn-файлов нет в {0}. Эта машина НЕ помечена Steam как Trusted '
             'Device — залогинься через VNC с 2FA-кодом сначала.'.format(STEAM_DIR))
    for p in ssfn:
        targets.append((p, Path(p).name))

    pulled = 0
    for remote, local_name in targets:
        sys.stderr.write('  {0:60s} ... '.format(remote))
        sys.stderr.flush()
        data = ga.file_read(remote)
        if data is None:
            sys.stderr.write('SKIP (не найден)\n')
            continue
        (out_dir / local_name).write_bytes(data)
        sys.stderr.write('OK ({0} B)\n'.format(len(data)))
        pulled += 1

    if pulled == 0:
        die('Ничего не забэкапил. Проверь что Steam установлен и хотя бы раз залогинен.')
    ok('Готово: {0} файл(ов) в {1}'.format(pulled, out_dir))


def cmd_restore(vm, src_vm=None):
    src = src_vm or vm
    src_dir = BACKUP_ROOT / src
    if not src_dir.exists():
        die('Нет бэкапа {0}. Запусти `./steam_backup.py backup {1}` сначала.'.format(src_dir, src))

    ga = GA(vm)
    if not ga.ping():
        die('GA не отвечает')

    info('Restore VM={0} ← {1}'.format(vm, src_dir))

    pushed = 0
    for local in sorted(src_dir.iterdir()):
        if not local.is_file():
            continue
        name = local.name
        # ssfn* кладём в корень Steam, vdf — в config/
        if name.startswith('ssfn'):
            remote = STEAM_DIR + '\\' + name
        elif name in ('loginusers.vdf', 'config.vdf'):
            remote = STEAM_CONFIG_DIR + '\\' + name
        else:
            warn('Неизвестный файл {0}, пропуск'.format(name))
            continue

        data = local.read_bytes()
        sys.stderr.write('  {0:60s} ... '.format(remote))
        sys.stderr.flush()
        if ga.file_write(remote, data):
            sys.stderr.write('OK ({0} B)\n'.format(len(data)))
            pushed += 1
        else:
            sys.stderr.write('FAIL\n')

    if pushed == 0:
        die('Ничего не подложил.')
    ok('Готово: {0} файл(ов). Steam НА VM нужно перезапустить, чтобы он перечитал config.'
       .format(pushed))


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ('-h', '--help'):
        sys.stderr.write(__doc__ or '')
        sys.exit(0)

    if len(argv) < 2 or argv[0] not in ('backup', 'restore'):
        sys.stderr.write(__doc__ or '')
        die('Ожидалось `backup <vm>` или `restore <vm> [--from <other_vm>]`', rc=2)

    cmd, vm = argv[0], argv[1]
    rest = argv[2:]

    if cmd == 'backup':
        if rest:
            warn('Лишние аргументы игнорируются: {0}'.format(rest))
        return cmd_backup(vm)

    src_vm = None
    if rest:
        if rest[0] == '--from' and len(rest) >= 2:
            src_vm = rest[1]
        else:
            die('Неизвестный аргумент: {0}'.format(rest), rc=2)
    return cmd_restore(vm, src_vm)


if __name__ == '__main__':
    main()
