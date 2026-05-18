#!/bin/bash
# prepare_vm.sh — скачивает нужное на хост, разворачивает в VM через QEMU GA
#
# Использование:
#   bash prepare_vm.sh <vm_name>
#   bash prepare_vm.sh vm013
#
# Что делает:
#   1. Скачивает ffmpeg (Windows x64 build от BtbN) на хост
#   2. Копирует через QEMU GA в VM:
#      - ffmpeg.exe                -> C:\benchmark\   (HTTP-deploy, для NVENC load)
#      - PsExec.exe                -> C:\benchmark\   (HTTP-deploy, cross-session запуск)
#      - run_benchmark.py          -> C:\benchmark\
#      - init_script_reconstructed.py -> C:\benchmark\
#      - cyberpunk_runner.py       -> C:\benchmark\
#      - run_via_ga.py             -> C:\benchmark\
#      - bench_psexec.bat          -> C:\benchmark\
#      - run_via_ga_launcher.bat   -> C:\benchmark\
#      - sitecustomize.py          -> C:\Program Files (x86)\Python36-32\lib\site-packages\
#   3. Проверяет что всё на месте

set -euo pipefail

VM="${1:?Usage: $0 <vm_name>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Цвета ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'

# %s для пользовательского текста — не интерпретирует \b/\n в путях.
# %b — только для ANSI-цветов (они хранятся как литералы \033[...m).
info()    { printf '%b[INFO]%b %s\n' "$CYAN"   "$NC" "$*"; }
ok()      { printf '%b[ OK ]%b %s\n' "$GREEN"  "$NC" "$*"; }
warn()    { printf '%b[WARN]%b %s\n' "$YELLOW" "$NC" "$*" >&2; }
die()     { printf '%b[FAIL]%b %s\n' "$RED"    "$NC" "$*" >&2; exit 1; }

# ── Конфиг ──────────────────────────────────────────────────────────────────
WORK_DIR="$SCRIPT_DIR/vm_deploy_cache"
FFMPEG_ZIP="$WORK_DIR/ffmpeg.zip"
FFMPEG_EXE="$WORK_DIR/ffmpeg.exe"
PSTOOLS_ZIP="$WORK_DIR/PSTools.zip"
PSEXEC_EXE="$WORK_DIR/PsExec.exe"

# BtbN: стабильный URL на latest essentials build (win64, gpl)
# essentials содержит только ffmpeg/ffprobe/ffplay — достаточно
FFMPEG_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

# Sysinternals PSTools — нужен только PsExec.exe для кросс-сессионного запуска
PSTOOLS_URL="https://download.sysinternals.com/files/PSTools.zip"

# Наши файлы (должны лежать рядом со скриптом)
declare -A LOCAL_FILES=(
    ["sitecustomize.py"]="$SCRIPT_DIR/sitecustomize.py"
    ["run_benchmark.py"]="$SCRIPT_DIR/run_benchmark.py"
    ["init_script_reconstructed.py"]="$SCRIPT_DIR/init_script_reconstructed.py"
    ["cyberpunk_runner.py"]="$SCRIPT_DIR/cyberpunk_runner.py"
    ["run_via_ga.py"]="$SCRIPT_DIR/run_via_ga.py"
    ["bench_psexec.bat"]="$SCRIPT_DIR/bench_psexec.bat"
    ["run_via_ga_launcher.bat"]="$SCRIPT_DIR/run_via_ga_launcher.bat"
)

# Куда класть в VM (JSON-пути с двойными бэкслешами)
declare -A VM_PATHS=(
    ["sitecustomize.py"]='C:\\Program Files (x86)\\Python36-32\\lib\\site-packages\\sitecustomize.py'
    ["run_benchmark.py"]='C:\\benchmark\\run_benchmark.py'
    ["init_script_reconstructed.py"]='C:\\benchmark\\init_script_reconstructed.py'
    ["cyberpunk_runner.py"]='C:\\benchmark\\cyberpunk_runner.py'
    ["run_via_ga.py"]='C:\\benchmark\\run_via_ga.py'
    ["bench_psexec.bat"]='C:\\benchmark\\bench_psexec.bat'
    ["run_via_ga_launcher.bat"]='C:\\benchmark\\run_via_ga_launcher.bat'
    ["ffmpeg.exe"]='C:\\benchmark\\ffmpeg.exe'
    ["PsExec.exe"]='C:\\benchmark\\PsExec.exe'
)

# ── HTTP fast-transfer (для больших бинарей: ffmpeg.exe, PsExec.exe) ────────
HTTP_PORT="${HTTP_PORT:-8765}"
HTTP_TIMEOUT_S=600         # 10 мин — с запасом на ffmpeg ~200MB по медленному каналу
HTTP_PID=""
HOST_IP=""

cleanup_http() {
    if [ -n "$HTTP_PID" ] && kill -0 "$HTTP_PID" 2>/dev/null; then
        kill "$HTTP_PID" 2>/dev/null || true
        wait "$HTTP_PID" 2>/dev/null || true
    fi
    HTTP_PID=""
}
trap cleanup_http INT TERM EXIT

# Определить IP хоста, который VM увидит как источник, и сможет нему обратно
# постучаться. Получаем IPv4 VM через QGA, дальше `ip route get <vm-ip>` —
# ядро вернёт src-IP, с которого пакеты пойдут к VM. По симметрии маршрута
# это и есть тот IP, по которому VM достучится до хоста.
#
# Этот способ работает для любого типа сети (bridge, NAT-network, direct,
# routed, macvtap), независимо от того, как называется интерфейс на хосте.
get_host_ip_for_vm() {
    local vm="$1" vm_ip host_ip
    vm_ip=$(virsh qemu-agent-command "$vm" \
        '{"execute":"guest-network-get-interfaces"}' 2>/dev/null \
        | jq -r '.return[]?
                 | select(.name | test("Loopback"; "i") | not)
                 | ."ip-addresses"[]?
                 | select(."ip-address-type"=="ipv4")
                 | ."ip-address"' \
        | head -1)
    if [ -z "$vm_ip" ]; then
        echo "  (не получил IPv4 от QGA — VM не загружена / нет адреса)" >&2
        return 1
    fi
    host_ip=$(ip -4 route get "$vm_ip" 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')
    if [ -z "$host_ip" ]; then
        echo "  (нет маршрута к $vm_ip — VM в изолированной сети без route)" >&2
        return 1
    fi
    echo "  VM IP: $vm_ip → host src IP: $host_ip" >&2
    echo "$host_ip"
}

start_http_server() {
    local dir="$1" port="$2"
    # Сразу скажем что слышим/не слышим этот порт ДО запуска python
    if ss -ltn "sport = :$port" 2>/dev/null | grep -q ":$port "; then
        echo "  порт $port уже занят на хосте (ss):" >&2
        ss -ltnp "sport = :$port" 2>&1 | tail -n +2 >&2
        return 1
    fi
    # Логи python складываем во временный файл — на случай падения покажем
    local logfile; logfile=$(mktemp -t pkbench-http.XXXXXX.log)
    # --directory появился только в Python 3.7. На OL7/RHEL7 python3 = 3.6,
    # поэтому делаем cd в субшелле + exec (чтобы $! был PID самого python).
    ( cd "$dir" && exec python3 -m http.server "$port" --bind 0.0.0.0 ) \
        >"$logfile" 2>&1 &
    local pid=$!
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "  python http.server упал. stderr/stdout:" >&2
        sed 's/^/    /' "$logfile" >&2
        rm -f "$logfile"
        return 1
    fi
    if ! ss -ltn "sport = :$port" 2>/dev/null | grep -q ":$port "; then
        echo "  процесс жив (pid=$pid) но порт $port не слушает. Логи:" >&2
        sed 's/^/    /' "$logfile" >&2
        kill "$pid" 2>/dev/null
        rm -f "$logfile"
        return 1
    fi
    rm -f "$logfile"
    echo "$pid"
}

# Выполнить PowerShell-скрипт в VM, дождаться выхода, вернуть exit code.
# Использует -EncodedCommand (UTF-16LE/base64) — обходит quoting hell.
# capture-output + guest-exec-status работают на современных qga.
qga_ps_wait() {
    local timeout_s="$1" ps_script="$2"
    local b64 req pid status ec out err
    b64=$(printf '%s' "$ps_script" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
    req=$(jq -nc --arg b "$b64" \
        '{execute:"guest-exec",arguments:{path:"powershell.exe",arg:["-NoProfile","-NonInteractive","-EncodedCommand",$b],"capture-output":true}}')
    pid=$(virsh qemu-agent-command "$VM" "$req" 2>/dev/null | jq -r '.return.pid // empty')
    [ -z "$pid" ] && return 1

    local elapsed=0
    while (( elapsed < timeout_s )); do
        status=$(virsh qemu-agent-command "$VM" \
            "$(jq -nc --argjson pid "$pid" '{execute:"guest-exec-status",arguments:{pid:$pid}}')" 2>/dev/null)
        [[ "$(echo "$status" | jq -r '.return.exited')" == "true" ]] && break
        sleep 2
        (( elapsed += 2 ))
    done
    [[ "$(echo "$status" | jq -r '.return.exited')" != "true" ]] && return 124

    ec=$(echo "$status" | jq -r '.return.exitcode // 0')
    out=$(echo "$status" | jq -r '.return."out-data" // empty' | base64 -d 2>/dev/null || true)
    err=$(echo "$status" | jq -r '.return."err-data" // empty' | base64 -d 2>/dev/null || true)
    [ -n "$out" ] && echo "$out"
    [ -n "$err" ] && echo "$err" >&2
    return "$ec"
}

# Залить файл в VM через HTTP. Сервер должен быть стартован, HOST_IP установлен.
# Файл должен лежать в каталоге, который раздаёт сервер (WORK_DIR).
# ga_http_put <local-path> <dst-win-path-single-bs> [label]
ga_http_put() {
    local src="$1" dst="$2"
    local label="${3:-$(basename "$src")}"
    local fname; fname=$(basename "$src")
    local size;  size=$(wc -c < "$src")
    printf "  %-45s %6d KB ... " "$label" $(( size / 1024 ))
    # Single-quoted в PowerShell = литерал, бэкслеши в пути не трогаются.
    # -UseBasicParsing убирает зависимость от IE; -OutFile стримит, не держит в памяти.
    local ps_script="
\$ProgressPreference = 'SilentlyContinue'
try {
    Invoke-WebRequest -Uri 'http://${HOST_IP}:${HTTP_PORT}/${fname}' -OutFile '${dst}' -UseBasicParsing
    exit 0
} catch {
    Write-Error \$_.Exception.Message
    exit 1
}"
    if qga_ps_wait "$HTTP_TIMEOUT_S" "$ps_script" >/dev/null 2>&1; then
        echo "OK"
        return 0
    else
        echo "FAIL"
        return 1
    fi
}

# ── Утилиты GA ──────────────────────────────────────────────────────────────
ga_ping() {
    virsh qemu-agent-command "$VM" '{"execute":"guest-ping"}' 2>/dev/null \
        | grep -q '"return"'
}

# Записать локальный файл в VM
ga_put() {
    local src="$1"
    local dst_json="$2"   # путь с двойными бэкслешами для JSON
    local label="${3:-$(basename "$src")}"

    local size
    size=$(wc -c < "$src")

    printf "  %-45s %6d KB ... " "$label" $(( size / 1024 ))

    local b64
    b64=$(base64 -w0 < "$src")

    local fopen
    fopen=$(virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-file-open\",\"arguments\":{\"path\":\"$dst_json\",\"mode\":\"wb\"}}" \
        2>/dev/null)

    local handle
    handle=$(echo "$fopen" | grep -o '"return":[0-9]*' | grep -o '[0-9]*')
    [ -z "$handle" ] && { echo "FAIL (open)"; return 1; }

    # Пишем чанками по 3 MB (base64 ~= 4MB строка)
    local chunk_size=$((3 * 1024 * 1024))
    local offset=0
    local total=${#b64}

    while [ $offset -lt $total ]; do
        local chunk="${b64:$offset:$chunk_size}"
        virsh qemu-agent-command "$VM" \
            "{\"execute\":\"guest-file-write\",\"arguments\":{\"handle\":$handle,\"buf-b64\":\"$chunk\"}}" \
            > /dev/null 2>&1
        offset=$(( offset + chunk_size ))
    done

    virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-file-close\",\"arguments\":{\"handle\":$handle}}" \
        > /dev/null 2>&1

    echo "OK"
}

# Создать директорию в VM (idempotent: cmd "if not exist ... md ...")
ga_mkdir() {
    local dir_json="$1"   # путь с \\ для JSON
    virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-exec\",\"arguments\":{\"path\":\"cmd.exe\",\"arg\":[\"/c\",\"if not exist $dir_json md $dir_json\"]}}" \
        > /dev/null 2>&1
    sleep 1
}

# Проверить существование файла в VM напрямую через guest-file-open.
# Возвращает "YES" если файл открылся (т.е. существует), иначе "NO".
# Прежняя версия гоняла cmd.exe + chk.tmp с race-condition на sleep 1.
ga_check_file() {
    local path_json="$1"   # путь с \\ литералом (станет \ при JSON-parse)
    local fopen handle
    fopen=$(virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-file-open\",\"arguments\":{\"path\":\"$path_json\",\"mode\":\"r\"}}" \
        2>/dev/null)
    handle=$(echo "$fopen" | grep -o '"return":[0-9]*' | grep -o '[0-9]*')
    if [ -z "$handle" ]; then
        echo "NO"
        return
    fi
    virsh qemu-agent-command "$VM" \
        "{\"execute\":\"guest-file-close\",\"arguments\":{\"handle\":$handle}}" \
        > /dev/null 2>&1
    echo "YES"
}

# ── STEP 0: Проверки ────────────────────────────────────────────────────────
echo
echo -e "${CYAN}══════════════════════════════════════════════${NC}"
echo -e "${CYAN}  VM Benchmark Deployment: $VM${NC}"
echo -e "${CYAN}══════════════════════════════════════════════${NC}"
echo

info "Проверка зависимостей на хосте..."
for cmd in virsh curl python3 unzip base64 jq iconv ip; do
    command -v "$cmd" > /dev/null || die "Не найдена утилита: $cmd"
done
ok "Все утилиты доступны"

info "Проверка QEMU Guest Agent..."
ga_ping || die "GA не отвечает. Проверь: virsh qemu-agent-command $VM '{\"execute\":\"guest-ping\"}'"
ok "Guest Agent отвечает"

info "Проверка локальных файлов..."
for name in "${!LOCAL_FILES[@]}"; do
    path="${LOCAL_FILES[$name]}"
    [ -f "$path" ] || die "Не найден файл: $path"
done
ok "Все локальные файлы на месте"

# ── STEP 1: Скачать ffmpeg ───────────────────────────────────────────────────
echo
echo -e "${CYAN}── Шаг 1: ffmpeg ───────────────────────────${NC}"

mkdir -p "$WORK_DIR"

if [ -f "$FFMPEG_EXE" ]; then
    ok "ffmpeg.exe уже скачан ($(du -sh "$FFMPEG_EXE" | cut -f1))"
else
    info "Скачиваем ffmpeg (BtbN win64-gpl)..."
    info "URL: $FFMPEG_URL"

    curl -L --progress-bar -o "$FFMPEG_ZIP" "$FFMPEG_URL" \
        || die "Ошибка скачивания ffmpeg"

    info "Распаковываем ffmpeg.exe..."
    # В архиве путь вида: ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe
    unzip -p "$FFMPEG_ZIP" "*/bin/ffmpeg.exe" > "$FFMPEG_EXE" \
        || die "Не удалось распаковать ffmpeg.exe"

    rm -f "$FFMPEG_ZIP"
    ok "ffmpeg.exe готов ($(du -sh "$FFMPEG_EXE" | cut -f1))"
fi

# ── STEP 1.5: Скачать PsExec ─────────────────────────────────────────────────
echo
echo -e "${CYAN}── Шаг 1.5: PsExec (Sysinternals) ──────────${NC}"

if [ -f "$PSEXEC_EXE" ]; then
    ok "PsExec.exe уже скачан ($(du -sh "$PSEXEC_EXE" | cut -f1))"
else
    info "Скачиваем PSTools.zip..."
    info "URL: $PSTOOLS_URL"

    curl -L --progress-bar -o "$PSTOOLS_ZIP" "$PSTOOLS_URL" \
        || die "Ошибка скачивания PSTools"

    info "Извлекаем PsExec.exe (32-bit, универсальный)..."
    unzip -p "$PSTOOLS_ZIP" "PsExec.exe" > "$PSEXEC_EXE" \
        || die "Не удалось извлечь PsExec.exe"

    rm -f "$PSTOOLS_ZIP"
    ok "PsExec.exe готов ($(du -sh "$PSEXEC_EXE" | cut -f1))"
fi

# ── STEP 2: Создать директорию в VM ─────────────────────────────────────────
echo
echo -e "${CYAN}── Шаг 2: Структура директорий в VM ────────${NC}"

info "Создаём C:\\benchmark\\ ..."
ga_mkdir 'C:\\benchmark'
ok "C:\\benchmark\\ готова"

# ── STEP 3: Копирование файлов ───────────────────────────────────────────────
echo
echo -e "${CYAN}── Шаг 3: Копирование файлов в VM ──────────${NC}"

# Мелочь (Python/bat/ps1, ~КБ) — через GA напрямую. Base64 over virtio-serial для
# таких размеров — это доли секунды, заводить HTTP смысла нет.
info "Копирую мелкие файлы через GA (base64)..."
for name in "${!LOCAL_FILES[@]}"; do
    src="${LOCAL_FILES[$name]}"
    dst="${VM_PATHS[$name]}"
    ga_put "$src" "$dst" "$name" || warn "Не удалось скопировать $name"
done

# Большие бинари (ffmpeg ~200MB, PsExec ~500KB) — через HTTP с хоста.
# GA-передача base64 через virtio-serial для 200MB занимает минуты;
# Invoke-WebRequest по host-bridge LAN — секунды.
echo
info "Запускаю временный HTTP-сервер для больших бинарей..."
HOST_IP=$(get_host_ip_for_vm "$VM") \
    || die "Не могу определить host IP для VM $VM (нет bridge?)"
info "  host IP (виден из VM): $HOST_IP"

HTTP_PID=$(start_http_server "$WORK_DIR" "$HTTP_PORT") \
    || die "HTTP-сервер не стартанул (порт $HTTP_PORT занят? попробуй HTTP_PORT=NNNN ...)"
ok "  HTTP server: pid=$HTTP_PID, port=$HTTP_PORT, dir=$WORK_DIR"

info "Качаю бинари в VM через Invoke-WebRequest..."
ga_http_put "$FFMPEG_EXE" 'C:\benchmark\ffmpeg.exe' 'ffmpeg.exe' \
    || warn "Не удалось скачать ffmpeg.exe (см. логи VM/Invoke-WebRequest)"
ga_http_put "$PSEXEC_EXE" 'C:\benchmark\PsExec.exe' 'PsExec.exe' \
    || warn "Не удалось скачать PsExec.exe"

cleanup_http
info "HTTP-сервер остановлен"

# ── STEP 4: Проверка ────────────────────────────────────────────────────────
echo
echo -e "${CYAN}── Шаг 4: Проверка файлов в VM ─────────────${NC}"

# Идём по VM_PATHS (тот же массив, что использовали при копировании), не дублируем.
# Превращаем JSON-формат пути (с \\) в "человеческий" (с \) для отображения.
all_ok=true
for name in "${!VM_PATHS[@]}"; do
    dst="${VM_PATHS[$name]}"
    dst_display=${dst//\\\\/\\}
    result=$(ga_check_file "$dst")
    if [ "$result" = "YES" ]; then
        ok "$dst_display"
    else
        warn "ОТСУТСТВУЕТ: $dst_display"
        all_ok=false
    fi
done

# ── Итог ────────────────────────────────────────────────────────────────────
echo
echo -e "${CYAN}══════════════════════════════════════════════${NC}"
if $all_ok; then
    ok "Deployment завершён успешно!"
else
    warn "Deployment завершён с предупреждениями"
fi
echo
echo "Запуск бенчмарка с хоста:"
echo -e "  ${YELLOW}# Idle (только Cyberpunk):${NC}"
echo "  bash bench_vm.sh $VM [vk|rt|2k]"
echo
echo -e "  ${YELLOW}# Production (Cyberpunk + NVENC 25 Mbps параллельно):${NC}"
echo "  bash bench_vm.sh $VM [vk|rt|2k] nvenc"
echo -e "${CYAN}══════════════════════════════════════════════${NC}"
