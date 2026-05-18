#!/usr/bin/env bash
# ga_cat.sh — `cat` для файла на VM через QEMU GA. На stdout.
#
# Usage:
#   bash ga_cat.sh <vm> <windows-path>
#   bash ga_cat.sh vm043 'C:\benchmark\last_ffmpeg.log'
#   bash ga_cat.sh vm043 'C:\benchmark\last_run.log'

set -eu

VM="${1:?Usage: $0 <vm> <windows-path>}"
WPATH="${2:?Usage: $0 <vm> <windows-path>}"

# Собираем JSON через python — экранирование бэкслешей надёжное.
build_open() {
    python3 -c "import json,sys; print(json.dumps({
        'execute':'guest-file-open',
        'arguments':{'path':sys.argv[1],'mode':'r'}
    }))" "$WPATH"
}

resp=$(virsh qemu-agent-command "$VM" "$(build_open)" 2>/dev/null || true)
handle=$(echo "$resp" | grep -o '"return":[0-9]*' | grep -o '[0-9]*' || true)
if [ -z "$handle" ]; then
    echo "ERROR: не могу открыть '$WPATH' на $VM (файла нет?)" >&2
    echo "GA ответ: $resp" >&2
    exit 1
fi

# Читаем чанками по 256 KB и собираем в base64-стрингу
content=""
chunk=$((256 * 1024))
while true; do
    resp=$(virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-file-read\",\"arguments\":{\"handle\":$handle,\"count\":$chunk}}" \
        2>/dev/null || true)
    b64=$(echo "$resp" | python3 -c "import sys,json
print(json.loads(sys.stdin.read()).get('return',{}).get('buf-b64',''))" 2>/dev/null || true)
    eof=$(echo "$resp" | python3 -c "import sys,json
print('1' if json.loads(sys.stdin.read()).get('return',{}).get('eof',False) else '0')" 2>/dev/null || true)
    content="${content}${b64}"
    [ "$eof" = "1" ] && break
    [ -z "$b64" ] && break
done

virsh qemu-agent-command "$VM" \
    "{\"execute\":\"guest-file-close\",\"arguments\":{\"handle\":$handle}}" \
    >/dev/null 2>&1 || true

echo "$content" | base64 -d
