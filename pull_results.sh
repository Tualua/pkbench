#!/bin/bash
# pull_results.sh — стягивает результаты Cyberpunk бенчмарка с VM
# Использование: bash pull_results.sh <vm_name> [output_dir]

VM="${1:?Usage: $0 <vm_name>}"
OUT="${2:-./results_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT"

PYTHON='C:\\Program Files (x86)\\Python36-32\\python.exe'

ga_write_py() {
    local remote="$1"
    local code="$2"
    local b64
    b64=$(printf '%s' "$code" | base64 -w0)
    local fopen
    fopen=$(virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-file-open\",\"arguments\":{\"path\":\"$remote\",\"mode\":\"w\"}}" 2>/dev/null)
    local handle
    handle=$(echo "$fopen" | grep -o '"return":[0-9]*' | grep -o '[0-9]*')
    [ -z "$handle" ] && { echo "ERROR: cant write $remote"; return 1; }
    virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-file-write\",\"arguments\":{\"handle\":$handle,\"buf-b64\":\"$b64\"}}" >/dev/null 2>&1
    virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-file-close\",\"arguments\":{\"handle\":$handle}}" >/dev/null 2>&1
}

ga_read_file() {
    local remote="$1"
    local local_path="$2"
    local fopen
    fopen=$(virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-file-open\",\"arguments\":{\"path\":\"$remote\",\"mode\":\"rb\"}}" 2>/dev/null)
    local handle
    handle=$(echo "$fopen" | grep -o '"return":[0-9]*' | grep -o '[0-9]*')
    [ -z "$handle" ] && return 1
    local chunk_size=$((3*1024*1024))
    > "$local_path"
    while true; do
        local fread
        fread=$(virsh qemu-agent-command "$VM" \
            "{\"execute\":\"guest-file-read\",\"arguments\":{\"handle\":$handle,\"count\":$chunk_size}}" 2>/dev/null)
        local b64
        b64=$(echo "$fread" | grep -o '"buf-b64":"[^"]*"' | cut -d'"' -f4)
        [ -z "$b64" ] && break
        echo "$b64" | base64 -d >> "$local_path"
        local eof
        eof=$(echo "$fread" | grep -o '"eof":[a-z]*' | cut -d: -f2)
        [ "$eof" = "true" ] && break
    done
    virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-file-close\",\"arguments\":{\"handle\":$handle}}" >/dev/null 2>&1
    [ -s "$local_path" ]
}

ga_run_py() {
    local pyfile="$1"
    local wait="${2:-5}"
    virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-exec\",\"arguments\":{\"path\":\"$PYTHON\",\"arg\":[\"$pyfile\"]}}" >/dev/null 2>&1
    sleep "$wait"
}

echo "=== Pull Cyberpunk results: $VM ==="
echo "=== Output: $OUT ==="
echo

# Пишем Python-скрипт который найдёт и скопирует все результаты в C:\temp\cp_results\
echo "Шаг 1: ищем результаты на VM..."

PYCODE='import os, shutil
src_base = r"C:\Users\gamer\Documents\CD Projekt Red\Cyberpunk 2077\benchmarkResults"
dst_base = r"C:\temp\cp_results"
if os.path.exists(dst_base):
    shutil.rmtree(dst_base)
os.makedirs(dst_base)
log = []
if os.path.exists(src_base):
    for run_dir in sorted(os.listdir(src_base)):
        run_path = os.path.join(src_base, run_dir)
        if not os.path.isdir(run_path):
            continue
        dst_dir = os.path.join(dst_base, run_dir)
        os.makedirs(dst_dir, exist_ok=True)
        for fname in os.listdir(run_path):
            src = os.path.join(run_path, fname)
            dst = os.path.join(dst_dir, fname)
            shutil.copy2(src, dst)
            log.append(dst)
else:
    log.append("ERROR: benchmarkResults not found")
open(r"C:\temp\find_results.log", "w").write("\n".join(log) + "\n")
'

ga_write_py 'C:\\\\temp\\\\find_results.py' "$PYCODE"
ga_run_py 'C:\\temp\\find_results.py' 5

echo "Шаг 2: читаем список файлов..."
ga_read_file 'C:\\\\temp\\\\find_results.log' "$OUT/find_results.log"

if [ ! -s "$OUT/find_results.log" ]; then
    echo "ERROR: лог пустой"
    exit 1
fi

echo "Найдены файлы:"
cat "$OUT/find_results.log"
echo

echo "Шаг 3: скачиваем..."
while IFS= read -r remote_path; do
    remote_path=$(echo "$remote_path" | tr -d '\r')
    [ -z "$remote_path" ] && continue
    echo "$remote_path" | grep -q "^ERROR" && { echo "  $remote_path"; continue; }

    run_dir=$(echo "$remote_path" | awk -F'\134' '{print $(NF-1)}')
    fname=$(echo "$remote_path" | awk -F'\134' '{print $NF}' | tr -d '\r')
    local_dir="$OUT/$run_dir"
    mkdir -p "$local_dir"

    remote_json=$(echo "$remote_path" | sed 's/\\/\\\\/g')

    printf "  %-55s ... " "$fname"
    if ga_read_file "$remote_json" "$local_dir/$fname"; then
        echo "OK ($(wc -c < "$local_dir/$fname") bytes)"
    else
        echo "FAIL"
    fi
done < "$OUT/find_results.log"

echo

echo "=== Результаты ==="
find "$OUT" -name "summary.json" | while read -r f; do
    echo "--- $(dirname "$f" | xargs basename) ---"
    python3 -c "
import json, sys
d = json.load(open('$f'))
data = d.get('Data', {})
print(f'  GPU        : {data.get(\"gpuName\")} ({data.get(\"gpuMemory\")} MB)')
print(f'  CPU        : {data.get(\"cpuName\")}')
print(f'  RAM        : {data.get(\"systemMemory\")} MB')
print(f'  Resolution : {data.get(\"renderWidth\")}x{data.get(\"renderHeight\")}')
print(f'  RT         : {data.get(\"rayTracingEnabled\")}  DLSS: {data.get(\"DLSSEnabled\")}')
print(f'  averageFps : {data.get(\"averageFps\", 0):.1f}')
print(f'  minFps     : {data.get(\"minFps\", 0):.1f}')
print(f'  maxFps     : {data.get(\"maxFps\", 0):.1f}')
" 2>/dev/null
    echo
done

echo "=== Готово: $OUT ==="
