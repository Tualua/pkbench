#!/usr/bin/env bash
#
# vm-set-ip.sh — назначить статический IPv4 Windows-гостю через qemu-guest-agent.
#
# Использование:
#   ./vm-set-ip.sh <vm-name> <ip>/<prefix> [<gateway>] [<dns1>[,<dns2>]]
#
# Примеры:
#   ./vm-set-ip.sh vm013 192.168.10.50/24 192.168.10.1 1.1.1.1,8.8.8.8
#   ./vm-set-ip.sh vm013 10.0.0.5/24 10.0.0.1
#   ./vm-set-ip.sh vm013 172.16.0.10/24
#
# Что делает:
#   1. Находит первый не-loopback интерфейс с IPv4 в госте.
#   2. Удаляет с него все существующие IPv4-адреса, выключает DHCP.
#   3. Устанавливает указанный IP/prefix.
#   4. Устанавливает gateway (если указан).
#   5. Устанавливает DNS (если указан).
#   6. Проверяет результат через guest-network-get-interfaces.

set -uo pipefail

VM="${1:-}"
CIDR="${2:-}"
GATEWAY="${3:-}"
DNS="${4:-}"

if [[ -z "$VM" || -z "$CIDR" ]]; then
  cat >&2 <<EOF
usage: $0 <vm-name> <ip>/<prefix> [<gateway>] [<dns1>[,<dns2>]]

examples:
  $0 vm013 192.168.10.50/24 192.168.10.1 1.1.1.1,8.8.8.8
  $0 vm013 10.0.0.5/24 10.0.0.1
  $0 vm013 172.16.0.10/24
EOF
  exit 1
fi

# Разобрать CIDR
if [[ ! "$CIDR" =~ ^([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)/([0-9]+)$ ]]; then
  echo "ERROR: bad CIDR format '$CIDR', expected like 192.168.1.10/24" >&2
  exit 1
fi
IP="${BASH_REMATCH[1]}"
PREFIX="${BASH_REMATCH[2]}"

if (( PREFIX < 1 || PREFIX > 32 )); then
  echo "ERROR: bad prefix $PREFIX, must be 1-32" >&2
  exit 1
fi

for bin in virsh jq base64 iconv; do
  command -v "$bin" >/dev/null || { echo "missing dependency: $bin" >&2; exit 1; }
done

#
# qga_ps_encoded <timeout> <powershell-script>
#   Запускает PowerShell-скрипт через -EncodedCommand (UTF-16LE base64).
#   Печатает stdout и stderr, возвращает exit code гостевого процесса.
#
qga_ps_encoded() {
  local timeout="$1"
  local script="$2"

  local b64
  b64=$(printf '%s' "$script" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)

  local req pid status out err ec
  req=$(jq -nc --arg b "$b64" \
    '{execute:"guest-exec",arguments:{path:"powershell.exe",arg:["-NoProfile","-NonInteractive","-EncodedCommand",$b],"capture-output":true}}')

  pid=$(virsh qemu-agent-command "$VM" "$req" 2>/dev/null | jq -r '.return.pid // empty')
  if [[ -z "$pid" ]]; then
    echo "qga_ps_encoded: failed to start powershell" >&2
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
    echo "qga_ps_encoded: timeout after ${timeout}s" >&2
    return 124
  fi

  ec=$(echo "$status" | jq -r '.return.exitcode // 0')
  out=$(echo "$status" | jq -r '.return."out-data" // empty' | base64 -d 2>/dev/null || true)
  err=$(echo "$status" | jq -r '.return."err-data" // empty' | base64 -d 2>/dev/null || true)

  [[ -n "$out" ]] && echo "$out"
  [[ -n "$err" ]] && echo "$err" >&2
  return "$ec"
}

# ---------------------------------------------------------------------------

echo "=== ping qga (vm=$VM) ==="
if ! virsh qemu-agent-command "$VM" '{"execute":"guest-ping"}' >/dev/null 2>&1; then
  echo "ERROR: qga not responding" >&2
  exit 1
fi

echo
echo "=== find active interface in guest ==="
# Берём первый не-loopback интерфейс, у которого уже есть IPv4 (т.е. активный).
# Если интерфейсов несколько — берём первый. Для твоего случая это ок.
IFACE_NAME=$(virsh qemu-agent-command "$VM" '{"execute":"guest-network-get-interfaces"}' \
  | jq -r '
      .return[]
      | select(.name | test("Loopback"; "i") | not)
      | select(."ip-addresses" // [] | map(select(."ip-address-type"=="ipv4")) | length > 0)
      | .name
    ' | head -1)

if [[ -z "$IFACE_NAME" ]]; then
  # Fallback — первый не-loopback вообще, даже без IP
  IFACE_NAME=$(virsh qemu-agent-command "$VM" '{"execute":"guest-network-get-interfaces"}' \
    | jq -r '.return[] | select(.name | test("Loopback"; "i") | not) | .name' | head -1)
fi

if [[ -z "$IFACE_NAME" ]]; then
  echo "ERROR: no usable interface found in guest" >&2
  exit 1
fi

echo "    interface: $IFACE_NAME"

echo
echo "=== current configuration ==="
virsh qemu-agent-command "$VM" '{"execute":"guest-network-get-interfaces"}' \
  | jq -r --arg name "$IFACE_NAME" '
      .return[]
      | select(.name == $name)
      | "  name:           \(.name)
  hardware-address: \(."hardware-address" // "?")
  ip-addresses:
\((."ip-addresses" // []) | map("    - \(."ip-address")/\(.prefix) [\(."ip-address-type")]") | join("\n"))"
    '

echo
echo "=== applying new configuration ==="
echo "    IP:      $IP/$PREFIX"
[[ -n "$GATEWAY" ]] && echo "    gateway: $GATEWAY"
[[ -n "$DNS" ]]     && echo "    DNS:     $DNS"

# PowerShell-скрипт назначения. Использует InterfaceAlias, не Index — так понятнее.
# DNS-список — массив, поэтому из bash передаём строку через запятую и в PS делаем split.
PS_SCRIPT=$(cat <<EOF
\$ErrorActionPreference = "Stop"
\$iface = "$IFACE_NAME"
\$ip = "$IP"
\$prefix = $PREFIX
\$gw = "$GATEWAY"
\$dnsList = "$DNS"

Write-Host "Targeting interface: \$iface"

# 1. Выключить DHCP на интерфейсе (на IPv4)
Write-Host "Disabling DHCP..."
Set-NetIPInterface -InterfaceAlias \$iface -AddressFamily IPv4 -Dhcp Disabled -ErrorAction SilentlyContinue

# 2. Удалить все существующие IPv4-адреса с интерфейса
Write-Host "Removing existing IPv4 addresses..."
Get-NetIPAddress -InterfaceAlias \$iface -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  ForEach-Object {
    Write-Host "  removing \$(\$_.IPAddress)/\$(\$_.PrefixLength)"
    Remove-NetIPAddress -InterfaceAlias \$iface -IPAddress \$_.IPAddress -AddressFamily IPv4 -Confirm:\$false -ErrorAction SilentlyContinue
  }

# 3. Удалить существующие default-routes на этом интерфейсе (иначе New-NetIPAddress может ругнуться)
Write-Host "Removing existing default routes on interface..."
Get-NetRoute -InterfaceAlias \$iface -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
  Remove-NetRoute -Confirm:\$false -ErrorAction SilentlyContinue

# 4. Назначить новый IP (с gateway или без)
Write-Host "Assigning \$ip/\$prefix..."
if (\$gw -and \$gw -ne "") {
  New-NetIPAddress -InterfaceAlias \$iface -IPAddress \$ip -PrefixLength \$prefix -DefaultGateway \$gw -AddressFamily IPv4 | Out-Null
} else {
  New-NetIPAddress -InterfaceAlias \$iface -IPAddress \$ip -PrefixLength \$prefix -AddressFamily IPv4 | Out-Null
}

# 5. DNS-серверы
if (\$dnsList -and \$dnsList -ne "") {
  \$servers = \$dnsList -split ","
  Write-Host "Setting DNS servers: \$(\$servers -join ', ')"
  Set-DnsClientServerAddress -InterfaceAlias \$iface -ServerAddresses \$servers
} else {
  # Если DNS не задан — сбросить на DHCP (хотя DHCP мы выключили, это просто очистит ручной список)
  Set-DnsClientServerAddress -InterfaceAlias \$iface -ResetServerAddresses
}

Write-Host "OK"
EOF
)

if ! qga_ps_encoded 60 "$PS_SCRIPT"; then
  echo "ERROR: configuration failed (see above)" >&2
  exit 1
fi

# Дать стеку устаканиться
sleep 2

echo
echo "=== verify ==="
virsh qemu-agent-command "$VM" '{"execute":"guest-network-get-interfaces"}' \
  | jq -r --arg name "$IFACE_NAME" '
      .return[]
      | select(.name == $name)
      | "  name:           \(.name)
  hardware-address: \(."hardware-address" // "?")
  ip-addresses:
\((."ip-addresses" // []) | map("    - \(."ip-address")/\(.prefix) [\(."ip-address-type")]") | join("\n"))"
    '

# Проверим, что наш IP действительно есть
HAVE_IP=$(virsh qemu-agent-command "$VM" '{"execute":"guest-network-get-interfaces"}' \
  | jq -r --arg ip "$IP" '
      [ .return[]."ip-addresses"[]? | select(."ip-address" == $ip) ] | length
    ')

if [[ "$HAVE_IP" -ge 1 ]]; then
  echo
  echo "Done. $IP is now configured on $IFACE_NAME."
else
  echo
  echo "WARNING: $IP not found in guest interface list. Configuration may have failed or hasn't propagated yet." >&2
  exit 2
fi
