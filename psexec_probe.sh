#!/usr/bin/env bash
# psexec_probe.sh — диагностика "Access denied" от PsExec.
#
# Usage:
#   bash psexec_probe.sh <vm>
#
# Гоняет 5 вариантов PsExec, после каждого читает probe.log из VM и печатает.
# Цель — изолировать причину: проблема с сессией (-i N) / именем пользователя
# (-u .\gamer) / ACL конкретно на python.exe / или PsExec в принципе не работает.
#
# Пароль gamer берётся из .gamer_pass.<vm> (сохраняется bench_vm.sh / vnc_prep.sh).

set -uo pipefail

VM="${1:?Usage: $0 <vm>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PASS_FILE="$SCRIPT_DIR/.gamer_pass.$VM"

[ -f "$PASS_FILE" ] || { echo "ERROR: $PASS_FILE не найден. Запусти сначала bench_vm.sh или vnc_prep.sh" >&2; exit 1; }
PASS=$(cat "$PASS_FILE")

# ── GA helpers (минимальные, без jq) ─────────────────────────────────────────
ga_send() { virsh qemu-agent-command "$VM" "$1" 2>/dev/null; }

ga_exec_cmdline() {
    # Запустить cmd /c "<cmdline>" в VM, не ждём результат
    local cmdline="$1"
    local payload
    payload=$(python3 -c '
import json, sys
print(json.dumps({
    "execute":"guest-exec",
    "arguments":{"path":"cmd.exe","arg":["/c", sys.argv[1]]}
}))
' "$cmdline")
    ga_send "$payload" >/dev/null
}

ga_file_exists() {
    local path="$1"
    local payload resp handle
    payload=$(python3 -c "import json,sys; print(json.dumps({'execute':'guest-file-open','arguments':{'path':sys.argv[1],'mode':'r'}}))" "$path")
    resp=$(ga_send "$payload")
    handle=$(echo "$resp" | grep -o '"return":[0-9]*' | grep -o '[0-9]*')
    [ -z "$handle" ] && return 1
    ga_send "{\"execute\":\"guest-file-close\",\"arguments\":{\"handle\":$handle}}" >/dev/null
    return 0
}

ga_read_file() {
    local path="$1"
    local payload resp handle b64 eof content="" chunk=$((64*1024))
    payload=$(python3 -c "import json,sys; print(json.dumps({'execute':'guest-file-open','arguments':{'path':sys.argv[1],'mode':'r'}}))" "$path")
    resp=$(ga_send "$payload")
    handle=$(echo "$resp" | grep -o '"return":[0-9]*' | grep -o '[0-9]*')
    [ -z "$handle" ] && return 1
    while true; do
        resp=$(ga_send "{\"execute\":\"guest-file-read\",\"arguments\":{\"handle\":$handle,\"count\":$chunk}}")
        b64=$(echo "$resp" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('return',{}).get('buf-b64',''))" 2>/dev/null)
        eof=$(echo "$resp" | python3 -c "import sys,json; print('1' if json.loads(sys.stdin.read()).get('return',{}).get('eof',False) else '0')" 2>/dev/null)
        content="${content}${b64}"
        [ "$eof" = "1" ] && break
        [ -z "$b64" ] && break
    done
    ga_send "{\"execute\":\"guest-file-close\",\"arguments\":{\"handle\":$handle}}" >/dev/null
    echo "$content" | base64 -d
}

# ── Запустить один probe и показать результат ────────────────────────────────
run_probe() {
    local label="$1" psexec_args="$2"
    echo
    echo "════════════════════════════════════════════════════════════════════"
    echo "  $label"
    echo "════════════════════════════════════════════════════════════════════"
    # удалим прошлый probe.log
    ga_exec_cmdline 'del /f /q C:\benchmark\probe.log >nul 2>&1'
    sleep 1
    # запуск
    ga_exec_cmdline "C:\\benchmark\\PsExec.exe -accepteula $psexec_args > C:\\benchmark\\probe.log 2>&1"
    sleep 5   # дать PsExec'у время отработать
    if ga_file_exists 'C:\benchmark\probe.log'; then
        ga_read_file 'C:\benchmark\probe.log' | sed 's/^/    /'
    else
        echo "    (probe.log не появился — PsExec не запустился вообще)"
    fi
}

echo "psexec_probe: VM=$VM"
echo "Каждый probe пишет в C:\\benchmark\\probe.log; PsExec обычно выводит туда"
echo "явное сообщение об ошибке вроде 'Access is denied' / 'Logon failure' / etc."

# 1. Baseline: PsExec работает в принципе? Запуск как SYSTEM без -i.
run_probe "[1] baseline: -s (как SYSTEM, без сессии gamer)" \
    "-s cmd.exe /c \"whoami > C:\\benchmark\\probe_who.txt\""

# 2. -u gamer, БЕЗ -i (запуск под gamer в session 0, без интерактива)
run_probe "[2] -u gamer без -i (auth работает? session 0)" \
    "-u gamer -p \"$PASS\" cmd.exe /c whoami"

# 3. -i 1 явно (предположение что gamer в session 1)
run_probe "[3] -u gamer -i 1 -d cmd.exe /c whoami (session 1 явно)" \
    "-u gamer -p \"$PASS\" -i 1 -d cmd.exe /c whoami"

# 4. -u .\gamer (форсим локальный домен — может помочь если есть AD-namespace конфликт)
run_probe "[4] -u .\\gamer -i 1 -d cmd.exe /c whoami" \
    "-u .\\gamer -p \"$PASS\" -i 1 -d cmd.exe /c whoami"

# 5. Тот самый python.exe который падал в bench_vm.sh — но через -i 1.
# Используем -V (просто версию) вместо -c "..." чтобы избежать вложенных
# кавычек/скобок которые ломают cmd-парсинг и дают ложный "Application text
# must be shorter than 32768 characters" от PsExec.
run_probe "[5] -u gamer -i 1 -d python.exe -V (без вложенных кавычек)" \
    "-u gamer -p \"$PASS\" -i 1 -d \"C:\\Program Files (x86)\\Python36-32\\python.exe\" -V"

# 6. То же что [5] плюс -h (elevated token) — на случай если python.exe имеет
# ACL требующий admin-прав. Должно работать, раз gamer в Administrators.
run_probe "[6] -u gamer -h -i 1 -d python.exe -V (с elevated token)" \
    "-u gamer -p \"$PASS\" -h -i 1 -d \"C:\\Program Files (x86)\\Python36-32\\python.exe\" -V"

echo
echo "════════════════════════════════════════════════════════════════════"
echo "Готово. Интерпретация:"
echo "  - probe 1 упал → PsExec вообще не работает в VM (Defender? карантин?)"
echo "  - probe 2 упал, 1 OK → проблема с auth gamer/паролем"
echo "  - probe 3 OK, 5 упал → проблема ACL на python.exe (нужен icacls)"
echo "  - probe 3 упал, 4 OK → нужен .\\gamer вместо gamer"
echo "  - probe 5 OK → меняй -i на -i 1 в bench_psexec.bat"
