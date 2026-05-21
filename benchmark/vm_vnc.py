#!/usr/bin/env python3
# vm_vnc.py — ad-hoc VNC доступ к bench-VM.
# Делает то же что VNC-фаза в pkbench.py, но без deploy/bench:
#   1. iptables flush на хосте (если root) — пробивка VNC
#   2. net user gamer <new_pw>      — свежий случайный пароль
#   3. net user /active:yes + Administrators
#   4. Windows Firewall TCP 5900    — открыть порт
#   5. sc start vncserver           — RealVNC service
#   6. Печать баннера: vm_ip:5900 / gamer / <pw>
#
# Использует GA + хелперы из pkbench.py напрямую (модуль импортится без
# побочных эффектов, потому что main() под if __name__ guard'ом).
"""vm_vnc.py — reset gamer password + setup VNC на libvirt VM."""

import sys
from pathlib import Path

# pkbench.py лежит рядом — добавляем dir в sys.path и импортим как модуль.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pkbench as pk


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ('-h', '--help'):
        sys.stderr.write(
            'Usage: vm_vnc.py <vm>\n'
            '\n'
            'Сгенерит новый случайный пароль gamer, активирует учётку,\n'
            'добавит в Administrators, откроет firewall TCP 5900, стартует\n'
            'RealVNC. Напечатает баннер с подключением.\n'
            '\n'
            'Под sudo — заодно сбросит iptables policy ACCEPT (нужно если\n'
            'VNC через хост не пробивается). Без sudo пропустит этот шаг.\n')
        sys.exit(2)
    if len(argv) != 1:
        pk.die('Ожидалось ровно `vm_vnc.py <vm>`, дано: ' + ' '.join(argv), rc=2)
    vm = argv[0]

    pk._iptables_flush_if_root()

    ga = pk.GA(vm)
    if not ga.ping():
        pk.die('GA не отвечает на VM {0} — qemu-ga не подключён или Windows '
               'GA не запущен'.format(vm))
    pk.ok('GA отвечает')

    pw = pk._generate_gamer_pass()
    pk.info('Применяю свежий пароль gamer (net user)...')
    # net.exe напрямую через arg-vector — НЕ через cmd /c (см. CLAUDE.md ловушки).
    rc, _, err = ga.exec_wait(r'C:\Windows\System32\net.exe',
                              ['user', 'gamer', pw], timeout=10)
    if rc != 0:
        pk.die('net user gamer <pw> упал rc={0}: {1}'.format(rc, err or ''))
    pk.ok('Пароль gamer обновлён')

    # Autologon — чтобы после reboot Windows сам зашёл под gamer'а и VNC-сессия
    # сразу была доступна, без ручного login через виртуальную консоль.
    pk._setup_autologon(ga, 'gamer', pw)

    pk._setup_vnc(ga, pw)

    vm_ip = ga.get_ipv4() or '<unknown>'
    sys.stderr.write('\n')
    sys.stderr.write('{0}══════════════════════════════════════════════════{1}\n'.format(pk._CYAN, pk._NC))
    sys.stderr.write('{0}  VNC ready{1}\n'.format(pk._GREEN, pk._NC))
    sys.stderr.write('    address : {0}:5900\n'.format(vm_ip))
    sys.stderr.write('    user    : gamer\n')
    sys.stderr.write('    pass    : {0}\n'.format(pw))
    sys.stderr.write('{0}══════════════════════════════════════════════════{1}\n'.format(pk._CYAN, pk._NC))


if __name__ == '__main__':
    main()
