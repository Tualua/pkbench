#!/bin/bash
# sync_to_host.sh — копирует файлы бенчмарка в /root/benchmark на хост-гипервизор
# по SSH/SCP. После этого на хосте можно дёргать prepare_vm.sh / bench_vm.sh
# / vnc_prep.sh / pull_results.sh относительно /root/benchmark.
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

# Файлы для копирования. Лежат рядом со скриптом.
FILES=(
    # Хост-сторонние скрипты деплоя/выкачки/запуска
    prepare_vm.sh
    pull_results.sh
    bench_vm.sh
    vnc_prep.sh

    # Python для VM
    run_benchmark.py
    init_script_reconstructed.py
    cyberpunk_runner.py
    run_via_ga.py
    sitecustomize.py

    # Windows-сторонние утилиты (хост пушит их в VM)
    simulate_nvenc_load.bat
    run_benchmark_with_nvenc.ps1
    bench_psexec.bat
    run_via_ga_launcher.bat
)

echo "=== Sync to $SSH_TARGET:$REMOTE_DIR ==="
echo

echo "Проверяю наличие локальных файлов..."
for f in "${FILES[@]}"; do
    [ -f "$SCRIPT_DIR/$f" ] || { echo "ERROR: не найден $SCRIPT_DIR/$f"; exit 1; }
done
echo "OK (${#FILES[@]} файлов)"
echo

echo "Создаю $REMOTE_DIR на $SSH_TARGET..."
ssh "$SSH_TARGET" "mkdir -p $REMOTE_DIR"
echo "OK"
echo

echo "Копирую..."
# -p: сохранить mtime/права; -C: сжатие в канале
scp -pC "${FILES[@]/#/$SCRIPT_DIR/}" "$SSH_TARGET:$REMOTE_DIR/"
echo

echo "=== Готово ==="
echo
echo "На хосте:"
echo "  cd $REMOTE_DIR"
echo "  bash prepare_vm.sh   <vm_name>            # деплой всех файлов в VM (вкл. PsExec)"
echo "  sudo bash vnc_prep.sh <vm_name>           # подготовить VNC для наблюдения (port 5900)"
echo "  bash bench_vm.sh     <vm_name> [vk|rt|2k] # запуск бенча через GA, JSON в stdout"
echo "  bash pull_results.sh <vm_name>            # выкачать сырые summary.json из VM"
