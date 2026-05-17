#!/usr/bin/env bash
#
# vnc_prep.sh — подготовить VM к VNC-наблюдению за бенчем.
#
# Извлечено из vm-vnc-prep.sh минимумом для нашего use-case: ни choco, ни
# disk tools — только VNC + firewall + iptables.
#
# Использование:
#   bash vnc_prep.sh <vm>
#   sudo bash vnc_prep.sh <vm>          # с iptables flush на хосте
#   NO_IPTABLES_FLUSH=1 bash vnc_prep.sh <vm>   # явно отключить flush
#
# Координация паролем с bench_vm.sh:
#   - Читает .gamer_pass.<vm> если есть, иначе генерит и сохраняет
#   - bench_vm.sh использует ту же логику — пароль стабилен между запусками,
#     VNC-сессия не рвётся при следующем bench_vm.sh
#
# Что делает:
#   1. Пингует GA
#   2. Обеспечивает пароль gamer (применяет на VM)
#   3. Запускает службу vncserver (RealVNC)
#   4. Активирует gamer + добавляет в Administrators
#   5. Открывает Windows Firewall на TCP 5900
#   6. (если root) Флашит iptables на хосте до ACCEPT по всем таблицам
#   7. Печатает IP гостя + статус 5900

set -uo pipefail

VM="${1:?Usage: $0 <vm>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PASS_FILE="$SCRIPT_DIR/.gamer_pass.$VM"
VNC_PORT=5900

for bin in virsh jq base64; do
    command -v "$bin" >/dev/null || { echo "missing dependency: $bin" >&2; exit 1; }
done

# ── QGA helpers (заимствовано из vm-vnc-prep.sh — используют capture-output) ──
qga_exec_to() {
    local timeout="$1" path="$2" args_json="$3"
    local req pid status out err ec
    req=$(jq -nc --arg path "$path" --argjson args "$args_json" \
        '{execute:"guest-exec",arguments:{path:$path,arg:$args,"capture-output":true}}')
    pid=$(virsh qemu-agent-command "$VM" "$req" 2>/dev/null | jq -r '.return.pid // empty')
    [[ -z "$pid" ]] && { echo "qga_exec: failed to start $path" >&2; return 1; }
    local i=0
    while (( i < timeout )); do
        status=$(virsh qemu-agent-command "$VM" \
            "$(jq -nc --argjson pid "$pid" '{execute:"guest-exec-status",arguments:{pid:$pid}}')" 2>/dev/null)
        [[ "$(echo "$status" | jq -r '.return.exited')" == "true" ]] && break
        sleep 1; ((i++))
    done
    [[ "$(echo "$status" | jq -r '.return.exited')" != "true" ]] && return 124
    ec=$(echo "$status" | jq -r '.return.exitcode // 0')
    out=$(echo "$status" | jq -r '.return."out-data" // empty' | base64 -d 2>/dev/null || true)
    err=$(echo "$status" | jq -r '.return."err-data" // empty' | base64 -d 2>/dev/null || true)
    [[ -n "$out" ]] && echo "$out"
    [[ -n "$err" ]] && echo "$err" >&2
    return "$ec"
}
qga_exec() { qga_exec_to 30 "$@"; }
qga_ps() {
    qga_exec "powershell.exe" \
        "$(jq -nc --arg c "$1" '["-NoProfile","-NonInteractive","-Command",$c]')"
}

# ── Пароль ───────────────────────────────────────────────────────────────────
ensure_password() {
    if [[ -f "$PASS_FILE" ]]; then
        cat "$PASS_FILE"
        return
    fi
    local p
    p=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24)
    umask 077
    echo "$p" > "$PASS_FILE"
    echo "$p"
}

# ── 1. Ping ──────────────────────────────────────────────────────────────────
echo "=== [1/7] GA ping (vm=$VM) ==="
virsh qemu-agent-command "$VM" '{"execute":"guest-ping"}' >/dev/null 2>&1 \
    || { echo "ERROR: GA not responding" >&2; exit 1; }
echo "    OK"

# ── 2. Пароль ────────────────────────────────────────────────────────────────
echo
echo "=== [2/7] gamer password (file: $PASS_FILE) ==="
PASS=$(ensure_password)
if [[ "$(wc -c < "$PASS_FILE")" -le 1 ]]; then
    echo "ERROR: пароль пустой" >&2; exit 1
fi
echo "    password: $PASS"

# ── 3. Старт VNC-службы ──────────────────────────────────────────────────────
echo
echo "=== [3/7] start RealVNC service ==="
qga_exec "C:\\Windows\\System32\\sc.exe" '["start","vncserver"]' || true
sleep 2
qga_ps "Get-Service vncserver | Select-Object Name,Status | Format-List | Out-String" || true

# ── 4. Применить пароль + активировать gamer + admin ─────────────────────────
echo
echo "=== [4/7] apply password / enable gamer / add to Administrators ==="
qga_exec "C:\\Windows\\System32\\net.exe" \
    "$(jq -nc --arg p "$PASS" '["user","gamer",$p]')"
qga_exec "C:\\Windows\\System32\\net.exe" '["user","gamer","/active:yes"]' || true
qga_exec "C:\\Windows\\System32\\net.exe" '["localgroup","Administrators","gamer","/add"]' || true

# ── 5. Firewall на VM ────────────────────────────────────────────────────────
echo
echo "=== [5/7] open Windows Firewall TCP $VNC_PORT ==="
qga_ps "if (-not (Get-NetFirewallRule -DisplayName 'VNC-Test' -ErrorAction SilentlyContinue)) { New-NetFirewallRule -DisplayName 'VNC-Test' -Direction Inbound -Protocol TCP -LocalPort $VNC_PORT -Action Allow -Profile Any | Out-Null }; Get-NetFirewallRule -DisplayName 'VNC-Test' | Select-Object DisplayName,Enabled,Action | Format-List | Out-String" \
    || qga_exec "C:\\Windows\\System32\\netsh.exe" \
       "[\"advfirewall\",\"firewall\",\"add\",\"rule\",\"name=VNC-Test\",\"dir=in\",\"action=allow\",\"protocol=TCP\",\"localport=$VNC_PORT\"]" \
       || true

# ── 6. iptables на хосте ─────────────────────────────────────────────────────
echo
echo "=== [6/7] host iptables flush ==="
if [[ "${NO_IPTABLES_FLUSH:-0}" == "1" ]]; then
    echo "    skipped (NO_IPTABLES_FLUSH=1)"
elif [[ $EUID -ne 0 ]]; then
    echo "    skipped: not root (запусти 'sudo bash vnc_prep.sh $VM' чтобы флашнуть iptables)"
else
    for table in filter nat mangle raw; do
        iptables -t "$table" -F 2>/dev/null || true
        iptables -t "$table" -X 2>/dev/null || true
    done
    for chain in INPUT FORWARD OUTPUT; do
        iptables -P "$chain" ACCEPT 2>/dev/null || true
    done
    iptables -t nat -P PREROUTING ACCEPT 2>/dev/null || true
    iptables -t nat -P POSTROUTING ACCEPT 2>/dev/null || true
    iptables -t nat -P OUTPUT ACCEPT 2>/dev/null || true
    echo "    iptables flushed (policy = ACCEPT)"
fi

# ── 7. IP + Listen-статус ────────────────────────────────────────────────────
echo
echo "=== [7/7] guest IP + TCP $VNC_PORT listen status ==="
echo "--- guest IPv4 ---"
virsh qemu-agent-command "$VM" '{"execute":"guest-network-get-interfaces"}' \
    | jq -r '.return[]
             | select(.name | test("Loopback"; "i") | not)
             | ."ip-addresses"[]?
             | select(."ip-address-type"=="ipv4")
             | "  \(.["ip-address"])/\(.prefix)"' || true

echo "--- TCP $VNC_PORT listen ---"
qga_ps "Get-NetTCPConnection -State Listen -LocalPort $VNC_PORT -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort | Format-List | Out-String" || true

echo
echo "=== Done ==="
echo "  VNC: подключайся к <guest-ip>:$VNC_PORT, user=gamer, pass=$PASS"
echo "  Пароль сохранён в $PASS_FILE — bench_vm.sh его переиспользует, VNC-сессия не порвётся."
