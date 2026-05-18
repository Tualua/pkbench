#!/bin/bash
# sync_to_host.sh — копирует pkbench.py и vm_bench.py на хост-гипервизор по
# SSH/SCP. После этого на хосте `python3 benchmark/pkbench.py <vm> [config]`
# делает весь флоу (deploy → VNC → bench → pull).
#
# Использование:
#   bash sync_to_host.sh <host>
#   bash sync_to_host.sh hv05.pk.minecolo.io
#   bash sync_to_host.sh root@hv05.pk.minecolo.io
#
# Можно передать SSH_USER через окружение, если не root:
#   SSH_USER=admin bash sync_to_host.sh hv05.pk.minecolo.io

set -euo pipefail

HOST="${1:?Usage: $0 <host>}"
SSH_USER="${SSH_USER:-root}"
REMOTE_DIR="/root/benchmark"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Если хост уже задан как user@host — не клеим SSH_USER ещё раз.
if [[ "$HOST" == *"@"* ]]; then
    SSH_TARGET="$HOST"
else
    SSH_TARGET="${SSH_USER}@${HOST}"
fi

# Только эти два файла нужны на хосте. Legacy (старые bash/py/bat) НЕ копируем —
# на хосте они либо уже есть от прошлого sync, либо не нужны вовсе. __pycache__
# с pyc'ами тоже не едет (явный список вместо `scp -r` всей папки).
FILES=(
    benchmark/pkbench.py
    benchmark/vm_bench.py
    benchmark/steam_backup.py
    benchmark/vm-set-ip.sh
)

# Скрипты с шебангом, на которые нужен +x на хосте.
EXECUTABLES=(
    pkbench.py
    steam_backup.py
    vm-set-ip.sh
)

echo "=== Sync to $SSH_TARGET:$REMOTE_DIR ==="
echo

echo "Проверяю локальные файлы..."
for f in "${FILES[@]}"; do
    [ -f "$SCRIPT_DIR/$f" ] || { echo "ERROR: не найден $SCRIPT_DIR/$f"; exit 1; }
done
echo "OK (${#FILES[@]} файлов)"
echo

# Кавычки вокруг $REMOTE_DIR на remote-стороне: shellcheck Info'у не нравится,
# что переменная подставляется на клиенте — намеренно (REMOTE_DIR константа,
# expand'ится один раз перед ssh, имя без пробелов).
echo "Создаю $REMOTE_DIR на $SSH_TARGET..."
ssh "$SSH_TARGET" "mkdir -p '$REMOTE_DIR'"
echo "OK"
echo

echo "Копирую..."
# -p: сохранить mtime/права; -C: сжатие в канале.
# Кладём плоско в $REMOTE_DIR (без подпапки benchmark/): локально файлы лежат
# в repo/benchmark/, но на хосте сам $REMOTE_DIR уже называется benchmark/ —
# подпапка дала бы /root/benchmark/benchmark/, что глупо. pkbench.py написан
# так, что SCRIPT_DIR — где он лежит, vm_deploy_cache/ тоже там же.
# ${FILES[@]/#/$SCRIPT_DIR/} = добавить $SCRIPT_DIR/ как префикс к каждому.
scp -pC "${FILES[@]/#/$SCRIPT_DIR/}" "$SSH_TARGET:$REMOTE_DIR/"

# Гарантируем +x на скриптах с шебангом. scp -p должен сохранить локальные права,
# но если локально кто-то снял или syncер запустили не с тех файловой системы —
# на хосте они всё равно запустятся как ./<script> благодаря shebang.
# ${EXECUTABLES[@]/#/$REMOTE_DIR/} = `/root/benchmark/pkbench.py /root/benchmark/steam_backup.py`
echo "Ставлю +x на ${#EXECUTABLES[@]} исполняемых..."
ssh "$SSH_TARGET" "chmod +x ${EXECUTABLES[*]/#/$REMOTE_DIR/}"
echo

echo "=== Готово ==="
echo
echo "На хосте:"
echo "  cd $REMOTE_DIR"
echo "  # Главная команда: deploy + VNC + бенч + pull одним заходом."
echo "  # Под sudo — добавляется iptables flush для пробивки VNC."
echo "  sudo ./pkbench.py <vm> [vk|rt|2k]"
echo
echo "  # Steam Trusted Device backup/restore (для обхода 2FA на свежих VM):"
echo "  ./steam_backup.py backup  <vm>"
echo "  ./steam_backup.py restore <vm> [--from <other_vm>]"
echo
echo "  # Отладочный read файла с VM в stdout:"
printf '  ./pkbench.py cat <vm> %s\n' "'C:\\benchmark\\last_run.log'"
