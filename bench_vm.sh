#!/bin/bash
# bench_vm.sh — end-to-end запуск Cyberpunk-бенчмарка на VM с хоста.
#
# Использование:
#   bash bench_vm.sh <vm_name> [config]
#       config: vk (default) | rt | 2k
#
# Что делает:
#   1. Сбрасывает пароль gamer на свежий случайный (VM эфемерная), сохраняет
#      в ~/.gamer_pass.<vm> рядом со скриптом
#   2. Чистит артефакты прошлого запуска в VM
#   3. Запускает run_via_ga.py через PsExec -u gamer -p <pass> -i -d
#      (инжектится в сессию gamer'а, где есть GPU + Steam + дисплей)
#   4. Поллит появление C:\benchmark\last_status.json (бенч кладёт его в конце)
#   5. Качает last_status.json + last_result.json через GA
#   6. На stdout печатает JSON результата, на stderr — прогресс
#
# Предусловия (выполняются prepare_vm.sh):
#   - PsExec.exe в C:\benchmark\
#   - run_benchmark.py, init_script_reconstructed.py, cyberpunk_runner.py,
#     run_via_ga.py — в C:\benchmark\
#   - sitecustomize.py — в site-packages
#   - Cyberpunk 2077 установлен, Steam залогинен под gamer
#
# Exit codes:
#   0 — успех, результат в stdout
#   1 — таймаут ожидания результата
#   2 — бенчмарк отработал, но с ошибкой (exit_code != 0)
#   3 — проблемы с предусловиями (GA / файлы / net user)

# pipefail НЕ ставим: наши пайплайны имеют вид `cmd | grep -q ...` или
# `tr ... | head -c N` — обычные паттерны, где tail-process штатно закрывает
# pipe и SIGPIPE-ит хвостовой. С pipefail+set -e это превращалось в тихий
# exit 141 без диагностики. Нам важен rc последней команды — это поведение
# по умолчанию без pipefail.
set -eu

# EXIT-trap: на случай если set -e всё-таки убьёт скрипт где-то по делу
# (typo, неинициализированная var) — печатает реальный rc, чтобы не молчало.
# Вызывается через trap, shellcheck это не видит.
# shellcheck disable=SC2317
on_exit() {
    local rc=$?
    [ "$rc" -ne 0 ] && echo "[bench_vm] ABORT rc=$rc (для трассы запусти 'bash -x bench_vm.sh ...')" >&2
}
trap on_exit EXIT

VM="${1:?Usage: $0 <vm_name> [config] [load]
  config: vk (default) | rt | 2k
  load:   idle (default) | nvenc — параллельная нагрузка NVENC через ffmpeg ddagrab}"
CONFIG="${2:-vk}"
LOAD="${3:-idle}"
case "$LOAD" in
    idle|nvenc) ;;
    *) echo "ERROR: load must be 'idle' or 'nvenc', got: $LOAD" >&2; exit 2 ;;
esac
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PASS_FILE="$SCRIPT_DIR/.gamer_pass.$VM"
POLL_INTERVAL_S=10
POLL_TIMEOUT_S=$((30 * 60))   # 2 итерации × ~8 мин + запас

# ── Логи (всё в stderr, stdout оставляем чистым под JSON-результат) ───────────
log()  { echo "[bench_vm] $*" >&2; }
die()  { log "FATAL: $*"; exit 3; }

# ── GA-помощники ──────────────────────────────────────────────────────────────

# Послать в GA готовый JSON-payload, вернуть raw response
ga_send() {
    local payload="$1"
    virsh qemu-agent-command "$VM" "$payload" 2>/dev/null
}

ga_ping() {
    ga_send '{"execute":"guest-ping"}' | grep -q '"return"'
}

# Запустить программу в VM. Аргумент — JSON-payload готовый. Возвращает PID.
ga_exec_payload() {
    local payload="$1"
    local resp
    resp=$(ga_send "$payload")
    echo "$resp" | grep -o '"pid":[0-9]*' | grep -o '[0-9]*'
}

# Удобный билдер payload для guest-exec через python (надёжное JSON-экранирование)
build_exec_payload() {
    local exe="$1"; shift
    python3 -c '
import json, sys
exe = sys.argv[1]
args = sys.argv[2:]
print(json.dumps({
    "execute": "guest-exec",
    "arguments": {"path": exe, "arg": args}
}))
' "$exe" "$@"
}

build_open_payload() {
    local path="$1"; local mode="$2"
    python3 -c '
import json, sys
print(json.dumps({
    "execute": "guest-file-open",
    "arguments": {"path": sys.argv[1], "mode": sys.argv[2]}
}))
' "$path" "$mode"
}

# Проверить существование файла (через open/close).
# ВНИМАНИЕ: если файла нет, GA возвращает error → virsh exit non-zero → с
# `set -e` падает САМ скрипт ДО `return 1`. Поэтому `|| true` на всех risky
# command sub'ах — нам нужен "файл есть/нет", а не остановка bench_vm.sh.
ga_file_exists() {
    local path="$1"
    local resp handle
    resp=$(ga_send "$(build_open_payload "$path" "r")" || true)
    handle=$(echo "$resp" | grep -o '"return":[0-9]*' | grep -o '[0-9]*' || true)
    [ -z "$handle" ] && return 1
    ga_send "{\"execute\":\"guest-file-close\",\"arguments\":{\"handle\":$handle}}" >/dev/null 2>&1 || true
    return 0
}

# Прочитать небольшой файл (до ~10 MB) и вывести в stdout.
# `|| true` на risky command sub'ах по той же причине что в ga_file_exists.
ga_read_file() {
    local path="$1"
    local resp handle
    resp=$(ga_send "$(build_open_payload "$path" "r")" || true)
    handle=$(echo "$resp" | grep -o '"return":[0-9]*' | grep -o '[0-9]*' || true)
    [ -z "$handle" ] && return 1

    local chunk_size=$((256 * 1024))   # 256 KB чанки
    local content=""
    while true; do
        local fread b64 eof
        fread=$(ga_send "{\"execute\":\"guest-file-read\",\"arguments\":{\"handle\":$handle,\"count\":$chunk_size}}" || true)
        b64=$(echo "$fread" | python3 -c "import sys,json
d=json.loads(sys.stdin.read()).get('return',{})
print(d.get('buf-b64',''))" 2>/dev/null || true)
        eof=$(echo "$fread" | python3 -c "import sys,json
d=json.loads(sys.stdin.read()).get('return',{})
print('1' if d.get('eof', False) else '0')" 2>/dev/null || true)
        content="${content}${b64}"
        [ "$eof" = "1" ] && break
        [ -z "$b64" ] && break   # safety: если что-то пошло не так
    done

    ga_send "{\"execute\":\"guest-file-close\",\"arguments\":{\"handle\":$handle}}" >/dev/null 2>&1 || true

    echo "$content" | base64 -d
}

# Удалить файл (через cmd del)
ga_delete() {
    local path="$1"
    local payload
    payload=$(build_exec_payload 'cmd.exe' '/c' "del /f /q \"$path\"")
    ga_send "$payload" >/dev/null
}

# ── 0. Pre-flight ─────────────────────────────────────────────────────────────
log "VM=$VM  config=$CONFIG"
log "Пинг GA..."
ga_ping || die "GA не отвечает (virsh qemu-agent-command $VM '{\"execute\":\"guest-ping\"}')"

log "Проверяю наличие нужных файлов в VM..."
for f in \
    'C:\benchmark\PsExec.exe' \
    'C:\benchmark\run_benchmark.py' \
    'C:\benchmark\run_via_ga.py' \
    'C:\benchmark\cyberpunk_runner.py' \
    'C:\benchmark\init_script_reconstructed.py'
do
    if ! ga_file_exists "$f"; then
        die "В VM нет $f. Запусти prepare_vm.sh $VM"
    fi
done

# ── 1. Пароль gamer ───────────────────────────────────────────────────────────
# Переиспользуем существующий пароль из .gamer_pass.<vm> (его мог положить
# vnc_prep.sh или прошлый запуск bench_vm.sh). Это критично для VNC: иначе
# каждый прогон бы кикал твою VNC-сессию.
if [ -f "$PASS_FILE" ]; then
    PASS=$(cat "$PASS_FILE")
    log "Использую существующий пароль gamer из $PASS_FILE"
else
    PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24)
    umask 077
    echo "$PASS" > "$PASS_FILE"
    log "Сгенерирован новый пароль gamer, сохранён в $PASS_FILE"
fi

log "Применяю пароль gamer на VM (net user)..."
# ВАЖНО: зовём net.exe НАПРЯМУЮ, не через `cmd /c "net user gamer \"PASS\""`.
# Через cmd /c с вложенными кавычками срабатывает quote-mangling: cmd с 4
# кавычками strip'ает первую и последнюю, net.exe видит args "PASS" (с
# литеральными кавычками) и устанавливает пароль ИМЕННО с кавычками вокруг.
# Тогда PsExec, который передаёт `-p PASS` (без кавычек, после ArgvW), получает
# auth failure. Каждый arg отдельной строкой → json.dumps + GA построят чистый
# cmdline → net.exe увидит args: user, gamer, PASS — без артефактов.
PAYLOAD=$(build_exec_payload 'C:\Windows\System32\net.exe' 'user' 'gamer' "$PASS")
pid=$(ga_exec_payload "$PAYLOAD")
[ -n "$pid" ] || die "guest-exec net user не отдал PID"
sleep 2   # дать net.exe досчитать

# ── 2. Чистим артефакты прошлого запуска ─────────────────────────────────────
log "Чистка артефактов в C:\\benchmark\\..."
for f in 'C:\benchmark\last_status.json' 'C:\benchmark\last_result.json' 'C:\benchmark\last_run.log'; do
    ga_delete "$f"
done

# ── 3. Запуск через PsExec (обёрнут в bench_psexec.bat для логирования) ─────
# Прямой вызов PsExec через GA в режиме -d "проглатывает" его stdout/stderr.
# Поэтому зовём .bat-обёртку, которая редиректит оба потока в psexec.log —
# его читаем сразу после, чтобы видеть что сказал PsExec (типичные сообщения:
# "session not found", "access denied", "could not start...").
log "Запуск через bench_psexec.bat (PsExec -u gamer -i -d ...)"
PAYLOAD=$(build_exec_payload 'cmd.exe' '/c' \
    'C:\benchmark\bench_psexec.bat' "$PASS" "$CONFIG" "$LOAD")
pid=$(ga_exec_payload "$PAYLOAD")
[ -n "$pid" ] || die "guest-exec bench_psexec.bat не отдал PID"
log "  cmd PID=$pid (отработает за ~1с)"

# PsExec в режиме -d пишет диагностику в psexec.log и сразу выходит.
# Даём пару секунд, читаем — это критично для дебага.
sleep 3
log "PsExec лог (C:\\benchmark\\psexec.log):"
if ga_file_exists 'C:\benchmark\psexec.log'; then
    ga_read_file 'C:\benchmark\psexec.log' | sed 's/^/    /' >&2
else
    log "  (psexec.log не появился — bench_psexec.bat не запустился вообще?)"
fi

# ── 4. Поллинг ───────────────────────────────────────────────────────────────
log "Жду last_status.json (timeout ${POLL_TIMEOUT_S}s, poll ${POLL_INTERVAL_S}s)..."
deadline=$(( $(date +%s) + POLL_TIMEOUT_S ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    if ga_file_exists 'C:\benchmark\last_status.json'; then
        log "  → статусный файл появился"
        break
    fi
    sleep "$POLL_INTERVAL_S"
done

if ! ga_file_exists 'C:\benchmark\last_status.json'; then
    log "ERROR: timeout (${POLL_TIMEOUT_S}s), last_status.json так и не появился"
    log "Подсказка: проверь VNC что игра вообще стартанула; PsExec мог не зайти в сессию gamer'а"
    exit 1
fi

# ── 5. Забираем результаты ────────────────────────────────────────────────────
log "Качаю last_status.json..."
STATUS_JSON=$(ga_read_file 'C:\benchmark\last_status.json')
echo "$STATUS_JSON" >&2

EXIT_CODE=$(echo "$STATUS_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['exit_code'])")

if [ "$EXIT_CODE" = "0" ]; then
    log "Бенчмарк завершился успешно, качаю last_result.json..."
    ga_read_file 'C:\benchmark\last_result.json'
    log "=== Готово ==="
    exit 0
else
    log "Бенчмарк завершился с exit_code=$EXIT_CODE, качаю last_run.log..."
    ga_read_file 'C:\benchmark\last_run.log' >&2 || log "(лог недоступен)"
    exit 2
fi
