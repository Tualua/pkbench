#!/usr/bin/env bash
#
# vm-vnc-prep.sh — подготовить Windows-гостя к VNC + установить инструменты бенчмаркинга.
#
# Что делает:
#   1.  Пингует qga.
#   2.  Запускает службу vncserver (RealVNC), если она не запущена.
#   3.  Сбрасывает пароль локальному пользователю gamer.
#   4.  Добавляет gamer в Administrators (на случай Permissions в RealVNC).
#   5.  Открывает порт 5900 в Windows Firewall.
#   6.  Сбрасывает все iptables-правила на хосте (filter, nat, mangle → ACCEPT).
#   7.  Устанавливает Chocolatey (если ещё нет).
#   8.  Устанавливает CrystalDiskMark + fio через choco.
#   9.  Показывает IP гостя и проверяет, что 5900 слушается.
#
# Использование:
#   sudo ./vm-vnc-prep.sh <vm-name> [<new-password>]
#
# По умолчанию новый пароль — "TestP@ss2026".
# Чтобы пропустить установку чоко/софта (только VNC):
#   SKIP_CHOCO=1 sudo ./vm-vnc-prep.sh <vm-name>

# Не используем errexit: qga_exec может вернуть ненулевой код от гостевой команды,
# и это нормально (например, sc.exe start для уже запущенной службы).
# Критические шаги обрабатываем явно.
set -uo pipefail

VM="${1:-}"
NEW_PASS="${2:-TestP@ss2026}"
GUEST_USER="gamer"
VNC_PORT="5900"
SKIP_CHOCO="${SKIP_CHOCO:-0}"

# Дольше для установки софта — choco тянет с интернета, может растянуться.
DEFAULT_TIMEOUT=30
LONG_TIMEOUT=600

if [[ -z "$VM" ]]; then
  echo "usage: $0 <vm-name> [<new-password>]" >&2
  exit 1
fi

for bin in virsh jq base64 iconv; do
  command -v "$bin" >/dev/null || { echo "missing dependency: $bin" >&2; exit 1; }
done

#
# qga_exec_to <timeout> <path> <json-args-array>
#   Запускает программу в госте, ждёт до <timeout> секунд, печатает stdout/stderr,
#   возвращает exitcode гостевого процесса.
#
qga_exec_to() {
  local timeout="$1"
  local path="$2"
  local args_json="$3"

  local req pid status out err ec
  req=$(jq -nc \
    --arg path "$path" \
    --argjson args "$args_json" \
    '{execute:"guest-exec",arguments:{path:$path,arg:$args,"capture-output":true}}')

  pid=$(virsh qemu-agent-command "$VM" "$req" 2>/dev/null | jq -r '.return.pid // empty')
  if [[ -z "$pid" ]]; then
    echo "qga_exec: failed to start $path" >&2
    return 1
  fi

  local i=0
  while (( i < timeout )); do
    status=$(virsh qemu-agent-command "$VM" \
      "$(jq -nc --argjson pid "$pid" '{execute:"guest-exec-status",arguments:{pid:$pid}}')" 2>/dev/null)
    if [[ "$(echo "$status" | jq -r '.return.exited')" == "true" ]]; then
      break
    fi
    sleep 1
    ((i++))
  done

  if [[ "$(echo "$status" | jq -r '.return.exited')" != "true" ]]; then
    echo "qga_exec: timeout after ${timeout}s waiting for pid=$pid ($path)" >&2
    return 124
  fi

  ec=$(echo "$status" | jq -r '.return.exitcode // 0')
  out=$(echo "$status" | jq -r '.return."out-data" // empty' | base64 -d 2>/dev/null || true)
  err=$(echo "$status" | jq -r '.return."err-data" // empty' | base64 -d 2>/dev/null || true)

  [[ -n "$out" ]] && echo "$out"
  [[ -n "$err" ]] && echo "$err" >&2
  return "$ec"
}

qga_exec() { qga_exec_to "$DEFAULT_TIMEOUT" "$@"; }

qga_ps() {
  local cmd="$1"
  qga_exec "powershell.exe" \
    "$(jq -nc --arg c "$cmd" '["-NoProfile","-NonInteractive","-Command",$c]')"
}

#
# qga_ps_encoded <timeout> <powershell-script>
#   Запускает произвольный PowerShell-скрипт через -EncodedCommand, обходя
#   проблемы с экранированием. Скрипт может быть многострочным.
#
qga_ps_encoded() {
  local timeout="$1"
  local script="$2"

  # PowerShell -EncodedCommand ожидает UTF-16LE base64
  local b64
  b64=$(printf '%s' "$script" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)

  qga_exec_to "$timeout" "powershell.exe" \
    "$(jq -nc --arg b "$b64" '["-NoProfile","-NonInteractive","-EncodedCommand",$b]')"
}

# ---------------------------------------------------------------------------

echo "=== [1/9] ping qga (vm=$VM) ==="
if ! virsh qemu-agent-command "$VM" '{"execute":"guest-ping"}' >/dev/null 2>&1; then
  echo "    ERROR: qga not responding" >&2
  exit 1
fi
echo "    qga alive"

echo
echo "=== [2/9] start RealVNC service ==="
qga_exec "C:\\Windows\\System32\\sc.exe" '["start","vncserver"]' || true
sleep 2
qga_ps "Get-Service vncserver | Select-Object Name,Status | Format-List | Out-String"

echo
echo "=== [3/9] set password for user $GUEST_USER ==="
qga_exec "C:\\Windows\\System32\\net.exe" \
  "$(jq -nc --arg u "$GUEST_USER" --arg p "$NEW_PASS" '["user",$u,$p]')"

echo
echo "=== [4/9] ensure $GUEST_USER is in Administrators and account is enabled ==="
qga_exec "C:\\Windows\\System32\\net.exe" \
  "$(jq -nc --arg u "$GUEST_USER" '["user",$u,"/active:yes"]')" || true
qga_exec "C:\\Windows\\System32\\net.exe" \
  "$(jq -nc --arg u "$GUEST_USER" '["localgroup","Administrators",$u,"/add"]')" || true

echo
echo "=== [5/9] open Windows Firewall for VNC ($VNC_PORT) ==="
qga_ps "if (-not (Get-NetFirewallRule -DisplayName 'VNC-Test' -ErrorAction SilentlyContinue)) { New-NetFirewallRule -DisplayName 'VNC-Test' -Direction Inbound -Protocol TCP -LocalPort $VNC_PORT -Action Allow -Profile Any | Out-Null }; Get-NetFirewallRule -DisplayName 'VNC-Test' | Select-Object DisplayName,Enabled,Action | Format-List | Out-String" \
  || qga_exec "C:\\Windows\\System32\\netsh.exe" \
       "[\"advfirewall\",\"firewall\",\"add\",\"rule\",\"name=VNC-Test\",\"dir=in\",\"action=allow\",\"protocol=TCP\",\"localport=$VNC_PORT\"]" \
       || true

echo
echo "=== [6/9] flush host iptables ==="
if [[ $EUID -ne 0 ]]; then
  echo "    WARNING: not root, skipping iptables flush" >&2
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
  echo "    iptables flushed, default policy = ACCEPT"
fi

if [[ "$SKIP_CHOCO" == "1" ]]; then
  echo
  echo "=== [7-8/9] SKIPPED (SKIP_CHOCO=1) ==="
else
  echo
  echo "=== [7/9] install Chocolatey (if missing) ==="
  # Проверяем напрямую файл, а не через PATH — qga живёт в session 0 без обновлённого PATH.
  CHECK_CHOCO='if (Test-Path "C:\ProgramData\chocolatey\bin\choco.exe") { Write-Host "FOUND" } else { Write-Host "MISSING" }'
  CHOCO_STATE=$(qga_ps_encoded 30 "$CHECK_CHOCO" || true)
  if [[ "$CHOCO_STATE" == *"FOUND"* ]]; then
    echo "    choco already installed, skipping"
    qga_ps_encoded 30 '& "C:\ProgramData\chocolatey\bin\choco.exe" --version' || true
  else
    echo "    installing chocolatey (may take 1-2 minutes)..."
    CHOCO_SCRIPT='Set-ExecutionPolicy Bypass -Scope Process -Force;
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072;
iex ((New-Object System.Net.WebClient).DownloadString("https://community.chocolatey.org/install.ps1"))'
    qga_ps_encoded "$LONG_TIMEOUT" "$CHOCO_SCRIPT" || {
      echo "    ERROR: chocolatey install failed" >&2
      exit 1
    }
    # Подтверждаем установку
    VERIFY=$(qga_ps_encoded 30 'if (Test-Path "C:\ProgramData\chocolatey\bin\choco.exe") { & "C:\ProgramData\chocolatey\bin\choco.exe" --version } else { Write-Host "NOT FOUND" }' || true)
    if [[ "$VERIFY" == *"NOT FOUND"* ]]; then
      echo "    ERROR: choco.exe not found after install" >&2
      exit 1
    fi
    echo "    chocolatey installed: $VERIFY"
  fi

  echo
  echo "=== [8/9] install CrystalDiskMark + fio ==="
  # --no-progress чтобы не засорять вывод; -y без интерактива; --limit-output короче.
  # При повторном запуске choco не переустанавливает уже стоящие пакеты — идемпотентно.
  INSTALL_SCRIPT='$choco = "C:\ProgramData\chocolatey\bin\choco.exe"
Write-Host "=== installing crystaldiskmark ==="
& $choco install crystaldiskmark -y --no-progress --limit-output
Write-Host "=== crystaldiskmark exit: $LASTEXITCODE ==="
Write-Host "=== installing fio ==="
& $choco install fio -y --no-progress --limit-output
Write-Host "=== fio exit: $LASTEXITCODE ==="
Write-Host "=== installed packages ==="
& $choco list --local-only crystaldiskmark fio'
  qga_ps_encoded "$LONG_TIMEOUT" "$INSTALL_SCRIPT" || {
    echo "    WARNING: install returned non-zero, software may still be partially installed" >&2
  }
fi

echo
echo "=== [9/9] verify ==="
echo "--- guest IPv4 addresses ---"
virsh qemu-agent-command "$VM" '{"execute":"guest-network-get-interfaces"}' \
  | jq -r '.return[]
           | select(.name | test("Loopback"; "i") | not)
           | ."ip-addresses"[]?
           | select(."ip-address-type"=="ipv4")
           | "  \(.["ip-address"])/\(.prefix)"' \
  || true

echo "--- TCP listen on $VNC_PORT ---"
qga_ps "Get-NetTCPConnection -State Listen -LocalPort $VNC_PORT -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort | Format-List | Out-String" \
  || true

if [[ "$SKIP_CHOCO" != "1" ]]; then
  echo "--- installed benchmark tools ---"
  qga_ps_encoded 30 '
$paths = @(
  "C:\Program Files\CrystalDiskMark*\DiskMark*.exe",
  "C:\ProgramData\chocolatey\lib\crystaldiskmark\tools\*\DiskMark*.exe",
  "C:\ProgramData\chocolatey\bin\fio.exe",
  "C:\Program Files\fio\fio.exe"
)
foreach ($p in $paths) {
  Get-Item $p -ErrorAction SilentlyContinue | Select-Object FullName, Length
}
' || true
fi

echo
echo "Done."
echo "  VNC:   <guest-ip>:$VNC_PORT  user=$GUEST_USER  pass=$NEW_PASS"
echo "  Tools: CrystalDiskMark in Start Menu; fio on PATH after relogin (or use full path)"

