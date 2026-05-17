#!/usr/bin/env bash
# bench_vm_diag.sh — диагностика: что происходит/не происходит в VM после bench_vm.sh.
#
# Usage: bash bench_vm_diag.sh <vm>
#
# Что показывает:
#   - наличие и содержимое всех артефактов (psexec.log, last_run.log, last_status.json)
#   - список релевантных процессов (cmd, python, Cyberpunk2077)
#   - факт наличия ключевых файлов (run_via_ga_launcher.bat и пр.)

set -uo pipefail

VM="${1:?Usage: $0 <vm>}"

for bin in virsh jq base64 iconv; do
    command -v "$bin" >/dev/null || { echo "missing dependency: $bin" >&2; exit 1; }
done

# ── QGA helpers (как в vm-vnc-prep.sh — capture-output + guest-exec-status) ──
qga_exec_to() {
    local timeout="$1" path="$2" args_json="$3"
    local req pid status out err ec
    req=$(jq -nc --arg path "$path" --argjson args "$args_json" \
        '{execute:"guest-exec",arguments:{path:$path,arg:$args,"capture-output":true}}')
    pid=$(virsh qemu-agent-command "$VM" "$req" 2>/dev/null | jq -r '.return.pid // empty')
    [[ -z "$pid" ]] && return 1
    local i=0
    while (( i < timeout )); do
        status=$(virsh qemu-agent-command "$VM" \
            "$(jq -nc --argjson pid "$pid" '{execute:"guest-exec-status",arguments:{pid:$pid}}')" 2>/dev/null)
        [[ "$(echo "$status" | jq -r '.return.exited')" == "true" ]] && break
        sleep 1; ((i++))
    done
    ec=$(echo "$status" | jq -r '.return.exitcode // 0')
    out=$(echo "$status" | jq -r '.return."out-data" // empty' | base64 -d 2>/dev/null || true)
    err=$(echo "$status" | jq -r '.return."err-data" // empty' | base64 -d 2>/dev/null || true)
    [ -n "$out" ] && echo "$out"
    [ -n "$err" ] && echo "$err" >&2
    return "$ec"
}

ga_ps() {
    qga_exec_to 30 "powershell.exe" \
        "$(jq -nc --arg c "$1" '["-NoProfile","-NonInteractive","-Command",$c]')"
}

show_file() {
    local label="$1" path="$2"
    echo
    echo "────── $label ──────"
    # Гоняем powershell вместо type — type через cmd может ломаться на cyrillic
    ga_ps "if (Test-Path '$path') { Get-Item '$path' | Format-List FullName,Length,LastWriteTime | Out-String; Get-Content '$path' -Raw -ErrorAction SilentlyContinue | Out-String } else { Write-Host 'НЕТ ФАЙЛА: $path' }"
}

echo "═══════════════════════════════════════════════════════"
echo "  bench_vm_diag: VM=$VM"
echo "═══════════════════════════════════════════════════════"

# ── 1. Процессы ──────────────────────────────────────────────────────────────
echo
echo "── Релевантные процессы на VM ──"
ga_ps "Get-Process | Where-Object {\$_.Name -match '^(cmd|python|Cyberpunk2077|PsExec|PSEXESVC|conhost)$'} | Select-Object Id,Name,SessionId,StartTime | Format-Table -AutoSize | Out-String"

# ── 2. Где какие файлы ───────────────────────────────────────────────────────
echo
echo "── Файлы в C:\\benchmark\\ ──"
ga_ps "Get-ChildItem C:\\benchmark\\ -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Format-Table Name,Length,LastWriteTime -AutoSize | Out-String"

# ── 3. Логи и артефакты ──────────────────────────────────────────────────────
show_file "psexec.log (вывод PsExec)" 'C:\benchmark\psexec.log'
show_file "last_run.log (stdout/stderr python)" 'C:\benchmark\last_run.log'
show_file "last_status.json (статус run_via_ga.py)" 'C:\benchmark\last_status.json'
show_file "last_result.json (результат бенча)" 'C:\benchmark\last_result.json'

# ── 4. Содержимое .bat (убедимся что задеплоились свежие версии) ─────────────
show_file "bench_psexec.bat" 'C:\benchmark\bench_psexec.bat'
show_file "run_via_ga_launcher.bat" 'C:\benchmark\run_via_ga_launcher.bat'

# ── 5. Проверка что python.exe доступен из сессии gamer'а ────────────────────
echo
echo "── Проверка пути python.exe ──"
ga_ps "if (Test-Path 'C:\\Program Files (x86)\\Python36-32\\python.exe') { 'python.exe ЕСТЬ' } else { 'python.exe НЕТ' }; Get-Acl 'C:\\Program Files (x86)\\Python36-32\\python.exe' 2>\$null | Select-Object -ExpandProperty Access | Where-Object {\$_.IdentityReference -like '*gamer*' -or \$_.IdentityReference -like '*Users*'} | Format-Table IdentityReference,FileSystemRights,AccessControlType -AutoSize | Out-String"

echo
echo "═══════════════════════════════════════════════════════"
echo "  Готово"
echo "═══════════════════════════════════════════════════════"
